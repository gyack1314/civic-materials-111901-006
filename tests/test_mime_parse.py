"""MIME 解析单元测试：编码文件名、嵌套原信、内嵌图片、签发链。"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from collector.mime_parse import parse_message, text_bodies

DATA = Path(__file__).resolve().parents[1] / 'data' / 'mail'


class MimeParseTests(unittest.TestCase):
    def test_rfc2231_encoded_filename_is_decoded(self):
        parsed = parse_message((DATA / 'initial.eml').read_bytes())
        attached = [p for p in parsed.parts if p.disposition == 'attachment']
        self.assertEqual(len(attached), 1)
        self.assertEqual(attached[0].filename, '年度披露-energy.txt')

    def test_nested_message_keeps_inner_sender_on_attachment(self):
        parsed = parse_message((DATA / 'forward.eml').read_bytes())
        containers = [p for p in parsed.parts if p.is_container]
        self.assertEqual(len(containers), 1)
        self.assertEqual(containers[0].envelope_from, 'reports@c02.invalid')
        nested_attachment = [
            p for p in parsed.parts
            if p.disposition == 'attachment'
            and p.content_type == 'application/octet-stream'][0]
        # 转发者不是原签发者：嵌套附件必须挂内层 From
        self.assertEqual(nested_attachment.envelope_from, 'reports@c02.invalid')
        self.assertGreaterEqual(nested_attachment.depth, 3)

    def test_inline_image_is_not_treated_as_attachment(self):
        parsed = parse_message((DATA / 'forward.eml').read_bytes())
        images = [p for p in parsed.parts if p.content_type == 'image/png']
        self.assertEqual(len(images), 1)
        self.assertNotEqual(images[0].disposition, 'attachment')
        self.assertEqual(images[0].cid, '<logo>')

    def test_outer_sender_used_for_root_attachments(self):
        parsed = parse_message((DATA / 'correction.eml').read_bytes())
        attached = [p for p in parsed.parts if p.disposition == 'attachment']
        self.assertTrue(attached)
        self.assertEqual(attached[0].envelope_from, 'reports@c01.invalid')

    def test_text_bodies_include_nested_layer_with_depth(self):
        parsed = parse_message((DATA / 'forward.eml').read_bytes())
        bodies = text_bodies(parsed)
        self.assertEqual(len(bodies), 2)  # 外层正文 + 内层正文
        self.assertTrue(all(isinstance(text, str) for _, text in bodies))


if __name__ == '__main__':
    unittest.main()
