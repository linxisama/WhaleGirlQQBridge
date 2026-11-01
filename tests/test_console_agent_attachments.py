"""Agent 附件：只读文本片段，二进制只给元数据，缺文件不崩。"""
import tempfile
import unittest
from pathlib import Path

from src.console_agent import attachment_context


class AttachmentTests(unittest.TestCase):
    def test_text_and_binary(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            text = root / 'note.md'; text.write_text('# 标题\n内容', encoding='utf-8')
            binary = root / 'a.bin'; binary.write_bytes(b'\x00\x01')
            out = attachment_context([str(text), str(binary), str(root / 'missing.txt')])
            self.assertIn('标题', out)
            self.assertIn('a.bin', out)
            self.assertIn('未读取正文', out)
            self.assertIn('文件不存在', out)

    def test_limits(self):
        out = attachment_context([f'/no/such/{i}.txt' for i in range(20)])
        self.assertLessEqual(len(out), 60000)


if __name__ == '__main__':
    unittest.main()
