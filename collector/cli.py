"""命令行入口。

只输出转义文本与 JSON，导出仅做字节复制；不渲染 HTML、不执行附件、
不访问邮件中的链接。
"""
from __future__ import annotations

import argparse
import json
import logging
import shutil
import sys
from pathlib import Path

from .collect import Collector
from .engine import PENDING_REASONS, Engine
from .gateway import Gateway
from .storage import Store

STATUS_LABEL = {
    'available': '可用',
    'withdrawn': '已确认撤回',
    'missing': '缺册',
    'pending': '待核实',
}


def _print_json(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=False))


def cmd_collect(args, store: Store) -> int:
    gateway = Gateway(args.base_url)
    collector = Collector(gateway, store)
    if args.once:
        stats = collector.collect_once()
        _print_json(stats)
    else:
        collector.run_forever(args.interval)
    return 0


def _project_payload(store: Store) -> list[dict]:
    engine = Engine(store)
    projected = engine.project()
    payload = []
    for key in sorted(projected):
        ps = projected[key]
        payload.append({
            'enterprise': ps.enterprise,
            'period': ps.period,
            'part': ps.part,
            'status': ps.status,
            'status_label': STATUS_LABEL[ps.status],
            'available_editions': ps.editions,
            'withdrawals': ps.withdrawals,
            'pending': [
                {**p, 'reason_label': PENDING_REASONS.get(p['reason'], p['reason'])}
                for p in ps.pending
            ],
        })
    return payload


def cmd_status(args, store: Store) -> int:
    payload = _project_payload(store)
    if args.json:
        _print_json(payload)
        return 0
    if not payload:
        print('台账为空，先执行 collect。')
        return 0
    for row in payload:
        print(f"[{row['status_label']}] {row['enterprise']} / "
              f"{row['period']} / {row['part']}")
        for edition in row['available_editions']:
            locs = ','.join(edition['evidence']['observed_at']) or '未观察'
            print(f"    可用版本 {edition['edition']}（签发 {edition['sender']}，"
                  f"来源 {locs}）")
        for w in row['withdrawals']:
            locs = ','.join(w['evidence']['observed_at']) or '未观察'
            print(f"    撤回版本 {w['edition']}（更正签发 {w['signer']}，"
                  f"来源 {locs}）")
        for p in row['pending']:
            print(f"    待核实：{PENDING_REASONS.get(p['reason'], p['reason'])}"
                  f" — {p['detail']}")
    return 0


def cmd_pending(args, store: Store) -> int:
    payload = [r for r in _project_payload(store) if r['status'] == 'pending']
    _print_json(payload)
    return 0


def cmd_decide(args, store: Store) -> int:
    decision_id = store.add_manual_decision(
        args.enterprise, args.period, args.part, args.scope_part,
        args.kind, args.rationale)
    store.commit()
    _print_json({'manual_decision_id': decision_id,
                 'enterprise': args.enterprise, 'period': args.period,
                 'part': args.part, 'scope_part': args.scope_part,
                 'kind': args.kind})
    return 0


def _resolve_blob(store: Store, digest_or_prefix: str) -> Path:
    # 存储布局 blobs/<前两位>/<完整 sha256>；完整哈希或任意非空前缀统一 glob
    prefix_dir = store.blobs / digest_or_prefix[:2]
    if prefix_dir.is_dir():
        matches = sorted(prefix_dir.glob(digest_or_prefix + '*'))
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise ValueError(f'blob 前缀 {digest_or_prefix} 匹配到多个文件')
    raise FileNotFoundError(digest_or_prefix)


def cmd_export(args, store: Store) -> int:
    """按 sha256（或其前缀）把原信/MIME 部分字节复制到目标目录，不解析执行。"""
    try:
        source = _resolve_blob(store, args.sha256)
    except (FileNotFoundError, ValueError) as exc:
        print(f'未找到 blob: {exc}', file=sys.stderr)
        return 2
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / (args.name or source.name + '.bin')
    shutil.copyfile(source, target)
    _print_json({'exported': str(target), 'sha256': source.name,
                 'bytes': target.stat().st_size})
    return 0


def cmd_evidence(args, store: Store) -> int:
    """列出某企业/期间/册别全部判断可回到的原信与 MIME 定位。"""
    rows = store.db.execute(
        'SELECT d.*, mp.envelope_from, mp.filename FROM disclosures d'
        ' JOIN mime_parts mp ON mp.id=d.part_id'
        ' WHERE d.enterprise=? AND d.period=? AND d.part=? ORDER BY d.id',
        (args.enterprise, args.period, args.part)).fetchall()
    corrections = store.db.execute(
        'SELECT * FROM corrections c WHERE c.target_part=? ORDER BY c.id',
        (args.part,)).fetchall()
    out = {'disclosures': [dict(r) for r in rows],
           'corrections': [dict(r) for r in corrections]}
    _print_json(out)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog='collector',
        description='ESG 往来邮件原件收集与更正版归属（只信任材料内部代码）')
    parser.add_argument('--state-dir', default='var/collector',
                        help='本地状态目录，默认 var/collector')
    sub = parser.add_subparsers(dest='command', required=True)

    p = sub.add_parser('collect', help='收一圈信（或持续轮询）')
    p.add_argument('--base-url', default='http://127.0.0.1:8765')
    p.add_argument('--once', action='store_true', help='收完一圈即退出')
    p.add_argument('--interval', type=float, default=60.0)
    p.set_defaults(func=cmd_collect)

    p = sub.add_parser('status', help='按企业/期间/册别展示当前状态')
    p.add_argument('--json', action='store_true')
    p.set_defaults(func=cmd_status)

    p = sub.add_parser('pending', help='只列待核实项（JSON）')
    p.set_defaults(func=cmd_pending)

    p = sub.add_parser('decide', help='登记带适用范围的人工确认/排除')
    p.add_argument('--enterprise', required=True)
    p.add_argument('--period', default=None)
    p.add_argument('--part', required=True)
    p.add_argument('--scope-part', default=None,
                   help='人工决定适用的册别范围；省略表示仅该册别')
    p.add_argument('--kind', choices=('confirm', 'exclude'), required=True)
    p.add_argument('--rationale', required=True)
    p.set_defaults(func=cmd_decide)

    p = sub.add_parser('export', help='按 sha256 导出原信/部分的原始字节')
    p.add_argument('sha256')
    p.add_argument('out_dir')
    p.add_argument('--name', default=None)
    p.set_defaults(func=cmd_export)

    p = sub.add_parser('evidence', help='回查某册别全部判断的证据定位')
    p.add_argument('--enterprise', required=True)
    p.add_argument('--period', required=True)
    p.add_argument('--part', required=True)
    p.set_defaults(func=cmd_evidence)
    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')
    args = build_parser().parse_args(argv)
    with Store(args.state_dir) as store:
        return args.func(args, store)


if __name__ == '__main__':
    raise SystemExit(main())
