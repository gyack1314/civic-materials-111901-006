"""披露文件与更正指令解析的严格性测试。"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from collector.materials import parse_correction, parse_disclosure


class DisclosureParseTests(unittest.TestCase):
    def test_valid_disclosure(self):
        result = parse_disclosure(
            b'enterprise=C01\nperiod=2025\npart=energy\nedition=v1\n')
        self.assertTrue(result.valid)
        self.assertEqual(result.disclosure.edition, 'v1')

    def test_misspelled_field_is_unknown_not_silently_accepted(self):
        result = parse_disclosure(
            b'enterprise=C01\nperiod=2025\npart=water\nedion=v1\n')
        self.assertFalse(result.valid)
        self.assertIn('edion', result.error)

    def test_missing_required_field(self):
        result = parse_disclosure(b'enterprise=C01\nperiod=2025\npart=energy\n')
        self.assertFalse(result.valid)
        self.assertIn('edition', result.error)

    def test_duplicate_key_rejected(self):
        result = parse_disclosure(
            b'enterprise=C01\nenterprise=C02\nperiod=2025\n'
            b'part=energy\nedition=v1\n')
        self.assertFalse(result.valid)
        self.assertIn('重复', result.error)

    def test_non_utf8_rejected(self):
        result = parse_disclosure(b'\xff\xfe\x00\x00not-text')
        self.assertFalse(result.valid)

    def test_garbage_line_rejected(self):
        result = parse_disclosure(b'no equals sign here\n')
        self.assertFalse(result.valid)


class CorrectionParseTests(unittest.TestCase):
    def test_structured_instruction_parsed(self):
        instruction = parse_correction(
            'replacement-of=v1\ntarget-part=energy\n能源表撤回，待补交。')
        self.assertEqual(instruction.replacement_of, 'v1')
        self.assertEqual(instruction.target_part, 'energy')

    def test_natural_language_alone_is_not_a_correction(self):
        self.assertIsNone(parse_correction('能源表撤回，稍后补交'))

    def test_half_instruction_is_not_a_correction(self):
        self.assertIsNone(parse_correction('replacement-of=v1\n仅口头说明'))

    def test_contradictory_duplicate_rejected(self):
        self.assertIsNone(
            parse_correction('replacement-of=v1\nreplacement-of=v2\n'
                             'target-part=energy\n'))


if __name__ == '__main__':
    unittest.main()
