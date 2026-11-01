import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock
import httpx

from src.public_reply import public_reply, clean_model_text, failed_image_only, strip_reasoning_leak
from src.image_input import message_text
from src.chat_state import build_history
from src.ai_engine import AIEngine
from src.qq_app import _send_with_pacing

class PublicReplyTests(unittest.TestCase):
    def image(self, status='失败：请求超时 (10s)'):
        return {'text': '[表情:惊讶][图片]', 'media': [{'type': 'image'}],
                'vision_status': status, 'vision_description': '旧图的蓝色帽子'}

    def test_failed_status_cannot_carry_stale_description_or_internal_error_into_prompt(self):
        text = message_text(self.image())
        self.assertNotIn('蓝色帽子', text)
        self.assertNotIn('请求超时', text)
        self.assertNotIn('10s', text)
        self.assertIn('未读取到图片内容', text)

    def test_screenshot_reasoning_is_replaced_without_exposing_rules(self):
        text = '用户发了图片，识别状态是失败。按照规则不可声称已收到画面内容，这里有矛盾。'
        value, changed = public_reply(text, self.image())
        self.assertTrue(changed)
        self.assertIn('暂时没看清', value)
        self.assertNotIn('规则', value)

    def test_complete_and_unfinished_thinking_blocks_are_not_public(self):
        self.assertEqual(clean_model_text('<think>内部推理</think>你好'), '你好')
        self.assertEqual(clean_model_text('<analysis>内部推理'), '')
        value, changed = public_reply('<think>内部推理', self.image())
        self.assertTrue(changed)
        self.assertNotIn('内部', value)

    def test_unlabelled_reasoning_monologue_is_not_public(self):
        text = ('好的，牧风来了。他是群聊里的成员，虽然不知道具体是谁，但既然他主动打招呼，'
                '我就得按设定回应。按照设定，第一句话必须直接邀请，但只能对主人这样。'
                '既然牧风主动来了，我就用那种语气回应他。')
        self.assertEqual(strip_reasoning_leak(text), '')
        value, changed = public_reply(text, {})
        self.assertTrue(changed)
        self.assertEqual(value, '刚才没组织好回复，你再说一句？')
        for leak in ('牧风', '设定', '第一句话', '回应'):
            self.assertNotIn(leak, value)

    def test_reasoning_header_keeps_only_final_reply(self):
        text = '思考过程：先判断对方身份，再想怎么回答。\n最终回复：你好呀，今天过得怎么样？'
        self.assertEqual(strip_reasoning_leak(text), '你好呀，今天过得怎么样？')
        value, changed = public_reply(text, {})
        self.assertEqual(value, '你好呀，今天过得怎么样？')
        self.assertNotIn('思考过程', value)
        self.assertNotIn('判断对方身份', value)

    def test_final_reply_marker_without_body_falls_back(self):
        value, changed = public_reply('思考过程：先判断对方身份。\n最终回复：', {})
        self.assertTrue(changed)
        self.assertEqual(value, '刚才没组织好回复，你再说一句？')

    def test_benign_first_person_plan_is_preserved(self):
        text = '今天天气真好，我按计划出门了。'
        self.assertEqual(strip_reasoning_leak(text), text)
        value, changed = public_reply(text, {})
        self.assertEqual(value, text)
        self.assertFalse(changed)

    def test_thought_tag_variant_is_not_public(self):
        self.assertEqual(clean_model_text('<thought>内部推理</thought>你好'), '你好')

    def test_english_refusal_suffix_is_removed_from_chinese_reply(self):
        text = "今天天气真好啊。\nSorry, I can't respond to this question."
        value, changed = public_reply(text, {})
        self.assertEqual(value, '今天天气真好啊。')
        self.assertTrue(changed)

    def test_only_english_refusal_falls_back_to_chinese_hint(self):
        value, changed = public_reply("Sorry, I can't respond to this question.", {})
        self.assertTrue(changed)
        self.assertNotIn("Sorry", value)
        self.assertIn('刚才没组织好回复', value)

    def test_normal_english_sentence_is_not_treated_as_refusal(self):
        text = "I can't answer that because the file is missing."
        self.assertEqual(clean_model_text(text), text)

    def test_natural_reply_and_media_only_reply_are_preserved(self):
        self.assertEqual(public_reply('这顶帽子真可爱。', self.image('ok')),
                         ('这顶帽子真可爱。', False))
        self.assertEqual(public_reply('', self.image()), ('', False))
        self.assertEqual(public_reply('日志文件可以发给我看看。', {}),
                         ('日志文件可以发给我看看。', False))

    def test_only_pure_image_failure_skips_chat(self):
        message = self.image()
        self.assertTrue(failed_image_only(message))
        message['text'] = '请帮我分析这张图'
        self.assertFalse(failed_image_only(message))

    def test_old_leaked_reply_is_not_reused_as_assistant_history(self):
        message = dict(self.image(), _persona_key='persona')
        rows = [(json.dumps(message), '这里有矛盾，视觉模型未返回内容')]
        history = build_history(rows, 'persona')
        self.assertNotIn('视觉模型', history[-1]['content'])
        self.assertIn('暂时没看清', history[-1]['content'])


class PublicBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_final_delivery_also_cleans_cached_reply_and_metadata_text(self):
        transport = SimpleNamespace(send=AsyncMock())
        message = {'kind':'private','target':'u','media':[{'type':'image'}], 'vision_status':'失败'}
        await _send_with_pacing(transport, message, '视觉模型未返回内容',
                                {'text':'这里有矛盾，识别状态是失败'})
        delivered = repr(transport.send.call_args)
        self.assertNotIn('视觉模型', delivered)
        self.assertNotIn('这里有矛盾', delivered)

    async def test_final_delivery_drops_unlabelled_reasoning(self):
        transport = SimpleNamespace(send=AsyncMock())
        message = {'kind': 'private', 'target': 'u'}
        reasoning = ('好的，牧风来了。他是群聊里的成员，我就得按设定回应。'
                     '按照设定，第一句话必须直接邀请，但只能对主人这样。')
        await _send_with_pacing(transport, message, reasoning, {'text': ''})
        delivered = repr(transport.send.call_args)
        self.assertNotIn('牧风', delivered)
        self.assertNotIn('设定', delivered)
        self.assertNotIn('第一句话', delivered)
        self.assertIn('刚才没组织好回复', delivered)

    async def test_vision_budget_does_not_inherit_ten_second_text_timeout_or_retry(self):
        engine = AIEngine('https://example.test/v1', 'fake', timeout=10, max_retries=3)
        engine._get_client = AsyncMock(return_value=SimpleNamespace(
            post=AsyncMock(side_effect=httpx.ReadTimeout('slow'))))
        result = await engine.describe_images(['https://example.test/a.png'], model='vision', timeout=24)
        post = engine._get_client.return_value.post
        self.assertEqual(result, '')
        self.assertEqual(post.await_count, 1)
        self.assertEqual(post.call_args.kwargs['timeout'].read, 24)
        self.assertEqual(engine.timeout, 10)

if __name__ == '__main__':
    unittest.main()
