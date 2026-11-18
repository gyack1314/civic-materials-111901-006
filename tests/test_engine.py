"""归属引擎测试：撤回与缺册区分、局部替换、转发身份、人工确认范围。

直接喂原信字节，不经过网关；状态全部从只追加台账投影，重算幂等。
"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from collector.engine import Engine
from collector.storage import Store

DATA = Path(__file__).resolve().parents[1] / 'data' / 'mail'


class EngineTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = Store(self._tmp.name)
        self.store.replace_contacts({
            'C01': ('reports@c01.invalid',),
            'C02': ('reports@c02.invalid',),
        })
        self.engine = Engine(self.store)

    def tearDown(self):
        self.store.close()
        self._tmp.cleanup()

    def ingest(self, name: str):
        return self.engine.ingest((DATA / name).read_bytes())

    def key(self, enterprise='C01', period='2025', part='energy'):
        return self.engine.project()[(enterprise, period, part)]


class IngestTests(EngineTestBase):
    def test_ingest_is_idempotent_on_same_bytes(self):
        first = self.ingest('initial.eml')
        second = self.ingest('initial.eml')
        self.assertFalse(first.reused)
        self.assertTrue(second.reused)

    def test_same_message_id_different_bytes_both_kept(self):
        self.ingest('initial.eml')
        self.ingest('forward.eml')
        rows = self.store.db.execute(
            'SELECT COUNT(*) n FROM raw_messages WHERE message_id=?',
            ('<same@local.invalid>',)).fetchone()
        self.assertEqual(rows['n'], 2)

    def test_initial_disclosure_is_available(self):
        self.ingest('initial.eml')
        ps = self.key()
        self.assertEqual(ps.status, 'available')
        self.assertEqual(ps.editions[0]['edition'], 'v1')


class WithdrawalTests(EngineTestBase):
    def test_withdrawal_is_distinct_from_missing(self):
        self.ingest('initial.eml')
        self.ingest('correction.eml')
        ps = self.key()
        self.assertEqual(ps.status, 'withdrawn')
        self.assertEqual(ps.withdrawals[0]['edition'], 'v1')
        # 证据回到具体原信和正文 MIME 层
        self.assertEqual(ps.withdrawals[0]['evidence']['mime_depth'], 1)

    def test_partial_replacement_does_not_touch_other_parts(self):
        self.ingest('initial.eml')
        self.ingest('correction.eml')
        water = self.key(part='water')
        self.assertEqual(water.status, 'available')
        energy = self.key(part='energy')
        self.assertEqual(energy.status, 'withdrawn')

    def test_moving_to_archive_is_not_a_withdrawal(self):
        # 只有 initial，没有更正信：移动文件夹本身不改变状态
        self.ingest('initial.eml')
        self.assertEqual(self.key().status, 'available')


class ForwardingTests(EngineTestBase):
    def test_forwarded_other_enterprise_file_stays_pending(self):
        self.ingest('initial.eml')
        self.ingest('correction.eml')
        self.ingest('forward.eml')
        ps = self.key()
        # v1 撤回的事实不被待核实候选冲掉
        self.assertEqual(ps.status, 'withdrawn')
        reasons = {p['reason'] for p in ps.pending}
        self.assertIn('attestation_pending', reasons)
        v2 = next(p for p in ps.pending if p.get('edition') == 'v2')
        self.assertIn('reports@c02.invalid', v2['detail'])
        # 证据定位到嵌套 MIME 层与具体部分
        self.assertEqual(v2['evidence']['mime_depth'], 3)
        self.assertEqual(v2['evidence']['mime_part_index'], 1)

    def test_forwarder_outer_address_is_not_recording_as_signer(self):
        self.ingest('forward.eml')
        row = self.store.db.execute(
            'SELECT mp.envelope_from FROM disclosures d'
            ' JOIN mime_parts mp ON mp.id=d.part_id').fetchone()
        self.assertEqual(row['envelope_from'], 'reports@c02.invalid')


class ManualDecisionTests(EngineTestBase):
    def test_later_mail_cannot_silently_override_manual_confirmation(self):
        self.ingest('initial.eml')
        self.ingest('correction.eml')
        self.store.add_manual_decision(
            'C01', '2025', 'energy', None, 'confirm', '人工核实仍有效')
        self.store.commit()
        ps = self.key()
        self.assertEqual(ps.status, 'pending')
        self.assertTrue(any(p['reason'] == 'manual_conflict' for p in ps.pending))
        # 撤回事实仍保留，没有被人工确认抹掉
        self.assertEqual(ps.withdrawals[0]['edition'], 'v1')

    def test_manual_scope_does_not_leak_to_other_parts(self):
        self.ingest('initial.eml')
        self.ingest('correction.eml')
        self.store.add_manual_decision(
            'C01', '2025', 'energy', None, 'confirm', '只针对能源表')
        self.store.commit()
        water = self.key(part='water')
        self.assertEqual(water.status, 'available')
        self.assertFalse(
            any(p['reason'] == 'manual_conflict' for p in water.pending))


class CorrectionScopeTests(EngineTestBase):
    def _correction_bytes(self, signer, replacement, target):
        # 无附件的纯更正信，用于制造无法落地/身份不符的情形
        return (
            f'From: {signer}\r\n'
            'To: research@local.invalid\r\n'
            'Message-ID: <x@local.invalid>\r\n'
            'MIME-Version: 1.0\r\n'
            'Content-Type: text/plain; charset="utf-8"\r\n\r\n'
            f'replacement-of={replacement}\r\ntarget-part={target}\r\n'
        ).encode()

    def test_unknown_signer_correction_stays_unresolved(self):
        self.ingest('initial.eml')
        self.engine.ingest(
            self._correction_bytes('stranger@unknown.invalid', 'v1', 'energy'))
        ps = self.key()
        self.assertEqual(ps.status, 'available')  # 未被撤回
        self.assertTrue(
            any(p['reason'] == 'correction_unresolved' for p in ps.pending))

    def test_correction_targeting_unknown_edition_does_not_withdraw(self):
        self.ingest('initial.eml')
        self.engine.ingest(
            self._correction_bytes('reports@c01.invalid', 'v9', 'energy'))
        ps = self.key()
        self.assertEqual(ps.status, 'available')
        self.assertTrue(
            any(p['reason'] == 'correction_unresolved' for p in ps.pending))


if __name__ == '__main__':
    unittest.main()
