"""T13 交付计划测试：预览/分段/独立幂等键/暂停/切号/文件兜底。"""
import unittest

from src.audio_contracts import ContractError, DeliveryState, ErrorCode
from src.audio_delivery_plan import (
    build_plan, can_continue, file_channel_fallback, mark_segment, next_pending,
)

ASSET = 'a' * 64


def plan(**over):
    base = {'audio_asset_ids': [ASSET], 'total_seconds': 200.0, 'kind': 'private',
            'target': '123456', 'account_id': '10001', 'request_id': 'req-1'}
    base.update(over)
    return build_plan(**base)


class DeliveryPlanTests(unittest.TestCase):
    def test_preview_is_limited_to_thirty_seconds(self):
        result = plan()
        self.assertEqual(len(result.segments), 1)
        self.assertEqual(result.segments[0].duration, 30.0)
        self.assertTrue(result.preview_only)

    def test_short_audio_preview_covers_whole_clip(self):
        result = plan(total_seconds=12.0)
        self.assertEqual(len(result.segments), 1)
        self.assertEqual(result.segments[0].duration, 12.0)

    def test_full_plan_segments_respect_the_upper_bound(self):
        result = plan(total_seconds=200.0, preview_only=False)
        self.assertGreater(len(result.segments), 1)
        for segment in result.segments:
            self.assertLessEqual(segment.duration, 60.0)
            self.assertGreater(segment.duration, 0)
        self.assertAlmostEqual(sum(s.duration for s in result.segments), 200.0, delta=0.01)

    def test_sentence_boundaries_are_preferred(self):
        result = plan(total_seconds=100.0, preview_only=False,
                      boundaries=[10.0, 20.0, 30.0])
        spans = [(s.start, s.end) for s in result.segments]
        self.assertEqual(spans[0], (0.0, 30.0))
        self.assertEqual(spans[1], (30.0, 90.0))

    def test_segment_request_ids_are_independent_and_stable(self):
        first = plan(total_seconds=200.0, preview_only=False)
        second = plan(total_seconds=200.0, preview_only=False)
        ids = [segment.request_id for segment in first.segments]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(ids, [segment.request_id for segment in second.segments])
        self.assertNotEqual(ids[0], 'req-1')

    def test_invalid_plans_are_rejected(self):
        with self.assertRaises(ContractError):
            plan(audio_asset_ids=[])
        with self.assertRaises(ContractError):
            plan(total_seconds=0)
        with self.assertRaises(ContractError):
            plan(total_seconds=9999)
        with self.assertRaises(ContractError):
            plan(kind='channel')
        with self.assertRaises(ContractError):
            plan(target='not-a-number')
        with self.assertRaises(ContractError):
            plan(request_id='')

    def test_uncertain_segment_pauses_following_segments(self):
        result = plan(total_seconds=200.0, preview_only=False)
        first = result.segments[0]
        mark_segment(result, first.index, DeliveryState.SENT, message_id='m-1')
        self.assertEqual(result.confirmed_segments(), [first.index])
        second = result.segments[1]
        mark_segment(result, second.index, DeliveryState.UNCERTAIN)
        self.assertTrue(result.paused)
        self.assertEqual(result.uncertain_segments(), [second.index])
        self.assertIsNone(next_pending(result))
        self.assertFalse(can_continue(result)[0])

    def test_confirmed_segments_cannot_be_overwritten_by_late_failure(self):
        result = plan(total_seconds=200.0, preview_only=False)
        first = result.segments[0]
        mark_segment(result, first.index, DeliveryState.SENT, message_id='m-1')
        mark_segment(result, first.index, DeliveryState.FAILED)
        self.assertIs(result.segments[0].state, DeliveryState.SENT)
        self.assertEqual(result.segments[0].message_id, 'm-1')

    def test_request_id_mismatch_is_rejected(self):
        result = plan()
        with self.assertRaises(ContractError):
            mark_segment(result, 0, DeliveryState.SENT, request_id='wrong')

    def test_account_change_stops_delivery(self):
        result = plan()
        allowed, reason = can_continue(result, account_id='99999')
        self.assertFalse(allowed)
        self.assertIn('账号', reason)
        self.assertIsNone(next_pending(result, account_id='99999'))

    def test_next_pending_returns_first_unsent(self):
        result = plan(total_seconds=200.0, preview_only=False)
        first = next_pending(result)
        self.assertEqual(first.index, 0)
        mark_segment(result, 0, DeliveryState.SENT)
        self.assertEqual(next_pending(result).index, 1)

    def test_payload_is_serializable_and_tracks_uncertain(self):
        result = plan(total_seconds=200.0, preview_only=False)
        mark_segment(result, 0, DeliveryState.SENT, message_id='m')
        mark_segment(result, 1, DeliveryState.UNCERTAIN)
        payload = result.to_payload()
        self.assertEqual(payload['confirmed'], [0])
        self.assertEqual(payload['uncertain'], [1])
        self.assertTrue(payload['paused'])
        self.assertIsInstance(payload['segments'], list)

    def test_file_channel_fallback_never_uploads_when_unverified(self):
        local = file_channel_fallback(200.0, verified_file_capability=False)
        self.assertEqual(local['deliver'], 'local_only')
        self.assertIn('未上传公网', local['note'])
        verified = file_channel_fallback(200.0, verified_file_capability=True)
        self.assertEqual(verified['deliver'], 'file_channel')


if __name__ == '__main__':
    unittest.main()
