"""桌宠资源包：校验、安装、列表；不启动真实窗口。"""
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from src import desktop_pet


def make_pack(folder: Path, identifier='whale', bad_frame=False):
    target = folder / f'{identifier}.zip'
    with zipfile.ZipFile(target, 'w') as archive:
        archive.writestr('pet.json', json.dumps({
            'format': 'whale-pet', 'version': 1, 'id': identifier,
            'name': '鲸鱼娘', 'fps': 8, 'scale': 0.8,
            'frames': ['frames/one.png']}, ensure_ascii=False))
        if not bad_frame:
            archive.writestr('frames/one.png', b'\x89PNG\r\n\x1a\n')
    return target


class DesktopPetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / 'root'
        self.root.mkdir()
        self.pack = make_pack(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def test_inspect_and_install(self):
        info = desktop_pet.inspect_pack(self.pack)
        self.assertEqual(info['id'], 'whale')
        self.assertEqual(info['frames'], ['frames/one.png'])
        with self.assertRaises(desktop_pet.PetPackError):
            desktop_pet.install_pack(self.root, self.pack)  # 未确认
        result = desktop_pet.install_pack(self.root, self.pack, confirmed=True)
        self.assertEqual(result['id'], 'whale')
        packs = desktop_pet.list_packs(self.root)
        self.assertEqual(packs[0]['name'], '鲸鱼娘')
        with self.assertRaises(desktop_pet.PetPackError):
            desktop_pet.install_pack(self.root, self.pack, confirmed=True)  # 同名

    def test_rejects_bad_path_and_missing_frame(self):
        bad = Path(self.tmp.name) / 'bad.zip'
        with zipfile.ZipFile(bad, 'w') as archive:
            archive.writestr('pet.json', json.dumps({
                'format': 'whale-pet', 'version': 1, 'id': 'x', 'frames': ['../x.png']}))
            archive.writestr('../x.png', b'x')
        with self.assertRaises(desktop_pet.PetPackError):
            desktop_pet.inspect_pack(bad)
        missing = make_pack(Path(self.tmp.name), 'missing', bad_frame=True)
        with self.assertRaises(desktop_pet.PetPackError):
            desktop_pet.inspect_pack(missing)

    def test_launch_uses_independent_process(self):
        desktop_pet.install_pack(self.root, self.pack, confirmed=True)
        process = type('P', (), {'pid': 12345})()
        with patch('src.desktop_pet.subprocess.Popen', return_value=process) as popen:
            result = desktop_pet.launch_pack(self.root, 'whale')
        self.assertEqual(result['pid'], 12345)
        self.assertTrue(any('pet-window' in str(arg) or 'src.pet_window' in str(arg) for arg in popen.call_args.args[0]))
        self.assertEqual(desktop_pet.stop_pack(self.root, 'whale')['stopped'], True)


if __name__ == '__main__':
    unittest.main()
