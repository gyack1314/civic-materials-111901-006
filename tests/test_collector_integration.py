"""对真实合成网关的集成测试：代际重建、409 重试、中断续收、两阶段语义。"""
import http.client
import json
import sys
import tempfile
import unittest
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from collector.collect import Collector
from collector.engine import Engine
from collector.gateway import Gateway
from collector.storage import Store
from source.server import running_source


def post_json(base: str, path: str, payload: dict) -> dict:
    parts = urlsplit(base)
    conn = http.client.HTTPConnection(parts.hostname, parts.port, timeout=10)
    conn.request('POST', path, json.dumps(payload).encode(),
                 {'Content-Type': 'application/json'})
    resp = conn.getresponse()
    body = resp.read()
    conn.close()
    return json.loads(body)


class GatewayIntegrationTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._ctx = running_source()
        self.base, self.source = self._ctx.__enter__()

    def tearDown(self):
        self._ctx.__exit__(None, None, None)
        self._tmp.cleanup()

    def _collector(self) -> tuple[Collector, Store]:
        store = Store(self._tmp.name)
        return Collector(Gateway(self.base), store), store

    def test_phase0_then_rebuild_phase1_full_semantics(self):
        collector, store = self._collector()
        stats0 = collector.collect_once()
        self.assertEqual(stats0['messages_new'], 2)

        projected = Engine(store).project()
        energy = projected[('C01', '2025', 'energy')]
        water = projected[('C01', '2025', 'water')]
        self.assertEqual(energy.status, 'withdrawn')   # 确认撤回 ≠ 缺册
        self.assertEqual(water.status, 'available')    # 局部撤回不连坐

        # 推进到阶段 1：文件夹代际重建
        post_json(self.base, '/__admin/advance', {'phase': 1, 'seconds': 5})
        stats1 = collector.collect_once()
        self.assertEqual(stats1['messages_new'], 1)    # 重建窗口新增的转发信
        self.assertEqual(stats1['messages_seen'], 2)   # 旧信重新枚举但字节已见
        self.assertGreaterEqual(stats1['conflicts'], 0)

        projected = Engine(store).project()
        energy = projected[('C01', '2025', 'energy')]
        self.assertEqual(energy.status, 'withdrawn')
        pending = next(p for p in energy.pending
                       if p['reason'] == 'attestation_pending')
        self.assertEqual(pending['edition'], 'v2')
        self.assertIn('reports@c02.invalid', pending['detail'])

        # 同 Message-ID 的两份不同字节都在
        self.assertEqual(
            store.db.execute(
                'SELECT COUNT(*) n FROM raw_messages WHERE message_id=?',
                ('<same@local.invalid>',)).fetchone()['n'], 2)
        # 移动前后的观察都在，且代际序号独立
        folders = {r['folder'] for r in store.db.execute(
            'SELECT DISTINCT folder FROM observations')}
        self.assertEqual(folders, {'INBOX', 'Archive'})
        store.close()

    def test_generation_409_forces_full_reenumeration(self):
        collector, store = self._collector()
        collector.collect_once()
        # 代际已变，仍拿旧 generation 直查必须收到 409
        post_json(self.base, '/__admin/advance', {'phase': 1})
        from collector.gateway import GenerationConflict
        with self.assertRaises(GenerationConflict):
            Gateway(self.base).mailbox('INBOX', 20, 0)
        # 收集循环自己处理代际变化：检查点代际更新且无遗漏
        stats = collector.collect_once()
        cp = store.checkpoint('INBOX')
        self.assertEqual(cp['generation'], 21)
        self.assertEqual(cp['through'], 2)
        self.assertEqual(stats['messages_new'], 1)
        store.close()

    def test_mid_enumeration_409_retries_and_loses_nothing(self):
        from collector.gateway import GenerationConflict

        collector, store = self._collector()
        real = collector.gateway
        base = self.base
        calls = {'n': 0}

        class FlakyGateway:
            def __init__(self):
                self.contacts = real.contacts
                self.folders = real.folders
                self.raw = real.raw

            def mailbox(self, folder, generation, after):
                calls['n'] += 1
                if calls['n'] == 1:
                    # 第一页刚返回就发生文件夹重建
                    post_json(base, '/__admin/advance',
                              {'phase': 1, 'seconds': 1})
                    raise GenerationConflict(folder, 21)
                return real.mailbox(folder, generation, after)

        collector.gateway = FlakyGateway()
        stats = collector.collect_once()
        self.assertGreaterEqual(stats['conflicts'], 1)
        # 重试按新代际整代枚举，重建窗口里的新信没有漏
        self.assertEqual(stats['messages_new'], 3)
        self.assertEqual(store.checkpoint('INBOX')['generation'], 21)
        store.close()

    def test_resume_after_interruption_keeps_facts_idempotent(self):
        collector, store = self._collector()
        collector.collect_once()
        post_json(self.base, '/__admin/advance', {'phase': 1})
        # 模拟状态目录保留、但检查点丢失（中断在推进检查点之前）
        store.db.execute('DELETE FROM checkpoints')
        store.commit()
        collector.collect_once()
        # 重新枚举不产生重复事实
        self.assertEqual(
            store.db.execute('SELECT COUNT(*) n FROM raw_messages')
            .fetchone()['n'], 3)
        self.assertEqual(
            store.db.execute('SELECT COUNT(*) n FROM disclosures')
            .fetchone()['n'], 3)
        self.assertEqual(
            store.db.execute('SELECT COUNT(*) n FROM corrections')
            .fetchone()['n'], 1)
        store.close()

    def test_second_cycle_without_new_mail_is_noop(self):
        collector, store = self._collector()
        collector.collect_once()
        again = collector.collect_once()
        self.assertEqual(again['messages_new'], 0)
        store.close()


if __name__ == '__main__':
    unittest.main()
