"""归属与状态判定引擎。

所有状态都从只追加事实表投影得到；ingest 对同一原信字节幂等。
判断依据只有材料内部披露代码、结构化更正与通讯录，主题/文件名/Message-ID
不参与版本判断。
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

from .materials import parse_correction, parse_disclosure
from .mime_parse import parse_message, text_bodies
from .storage import Store

# 待核实原因码 -> 中文说明
PENDING_REASONS = {
    'disclosure_invalid': '披露文件字段不全或无法解析',
    'attestation_pending': '签发者与声明企业通讯录不符',
    'correction_unresolved': '更正声明无法落到已知册别版本',
    'manual_conflict': '与已有人工确认范围冲突，需人工处理',
}


@dataclass
class IngestResult:
    raw_sha256: str
    reused: bool
    parts: int = 0
    disclosures: int = 0
    invalid_disclosures: int = 0
    corrections: int = 0


@dataclass
class PartStatus:
    enterprise: str
    period: str
    part: str
    status: str                       # available | withdrawn | missing | pending
    editions: list[dict] = field(default_factory=list)
    pending: list[dict] = field(default_factory=list)
    withdrawals: list[dict] = field(default_factory=list)


class Engine:
    def __init__(self, store: Store):
        self.store = store

    # ------------------------------------------------------------------
    # 入账
    # ------------------------------------------------------------------
    def ingest(self, raw: bytes) -> IngestResult:
        digest = hashlib.sha256(raw).hexdigest()
        if self.store.raw_exists(digest):
            return IngestResult(digest, reused=True)
        # 整封原信字节先入内容寻址存储，证据链从原信开始，不只存 MIME 叶子
        self.store.put_blob(raw)
        parsed = parse_message(raw)
        self.store.add_raw(digest, parsed.message_id, len(raw))

        result = IngestResult(digest, reused=False)
        for part in parsed.parts:
            blob_digest = self.store.put_blob(part.blob)
            part_id = self.store.add_part(
                digest, part.depth, part.index, part.content_type,
                part.filename, part.disposition, part.cid, blob_digest,
                part.envelope_from, part.is_container)
            # 只有显式附件才可能是披露文件；正文与内嵌图片不算，
            # “正文说有附件”不产生任何披露事实。
            if part.is_container or part.disposition != 'attachment':
                continue
            if part.content_type.startswith('image/'):
                continue
            outcome = parse_disclosure(part.blob)
            fields = None
            if outcome.valid:
                d = outcome.disclosure
                fields = {'enterprise': d.enterprise, 'period': d.period,
                          'part': d.part, 'edition': d.edition}
                result.disclosures += 1
            else:
                result.invalid_disclosures += 1
            self.store.add_disclosure(
                part_id, digest, part.depth, part.index,
                outcome.valid, outcome.error, fields, blob_digest)
            result.parts += 1

        for depth, body in text_bodies(parsed):
            instruction = parse_correction(body)
            if instruction is None:
                continue
            # 正文所在层的承载发件人即该层 MIME 部分记录的 envelope_from；
            # 嵌套层正文用内层 From，转发者不自动成为更正签发者。
            layer_senders = {
                p.depth: (p.envelope_from or '')
                for p in parsed.parts
                if p.content_type == 'text/plain' and not p.is_container
            }
            signer = layer_senders.get(depth, parsed.sender)
            excerpt = body.strip().replace('\n', ' | ')[:200]
            self.store.add_correction(
                digest, depth, signer, instruction.replacement_of,
                instruction.target_part, excerpt)
            result.corrections += 1
        return result

    # ------------------------------------------------------------------
    # 投影
    # ------------------------------------------------------------------
    def _contacts_lookup(self) -> dict[str, str]:
        """地址 -> 所属企业（同一地址只属于一个企业时才成立）。"""
        lookup: dict[str, str] = {}
        for enterprise, addresses in self.store.contact_enterprises().items():
            for address in addresses:
                lookup[address] = enterprise
        return lookup

    def _part_sender(self, row) -> str:
        part = self.store.db.execute(
            'SELECT envelope_from FROM mime_parts WHERE id=?',
            (row['part_id'],)).fetchone()
        return (part['envelope_from'] or '').lower()

    def project(self) -> dict[str, PartStatus]:
        """返回 {(enterprise, period, part): PartStatus}。"""
        contacts = self._contacts_lookup()
        statuses: dict[tuple, PartStatus] = {}

        def bucket(enterprise: str, period: str, part: str) -> PartStatus:
            key = (enterprise, period, part)
            if key not in statuses:
                statuses[key] = PartStatus(enterprise, period, part, 'missing')
            return statuses[key]

        # 1) 披露候选：有效且身份匹配才是可用候选，其余进待核实
        valid_editions: dict[tuple, list[dict]] = {}
        rows = self.store.db.execute(
            'SELECT * FROM disclosures ORDER BY id').fetchall()
        for row in rows:
            evidence = self._evidence(row['raw_sha256'], row['depth'],
                                      row['part_index'])
            if not row['valid']:
                # 无法归属到企业/期间时，挂在保留的原始线索下，仍可回查
                ent = row['enterprise'] or '（文件内无法识别）'
                per = row['period'] or '未知期间'
                prt = row['part'] or '未知册别'
                b = bucket(ent, per, prt)
                b.pending.append({
                    'reason': 'disclosure_invalid',
                    'detail': row['parse_error'],
                    'edition': row['edition'], 'evidence': evidence,
                })
                continue
            b = bucket(row['enterprise'], row['period'], row['part'])
            sender = self._part_sender(row)
            entry = {'edition': row['edition'], 'sender': sender,
                     'evidence': evidence, 'blob_sha256': row['blob_sha256']}
            if contacts.get(sender) == row['enterprise']:
                bucket_key = (row['enterprise'], row['period'], row['part'])
                known = valid_editions.setdefault(bucket_key, [])
                # 同 edition 的重复披露只保留一条，版本以内部代码为准
                if not any(e['edition'] == row['edition'] for e in known):
                    known.append(entry)
            else:
                b.pending.append({
                    'reason': 'attestation_pending',
                    'detail': f'签发地址 {sender or "（空）"} 不在 '
                              f'{row["enterprise"]} 通讯录',
                    'edition': row['edition'], 'evidence': evidence,
                })

        # 2) 结构化更正：身份、范围、版本都相符才构成确认撤回
        withdrawals: dict[tuple, list[dict]] = {}
        for c in self.store.db.execute('SELECT * FROM corrections ORDER BY id'):
            signer_enterprise = contacts.get(c['signer'])
            resolved = None
            if signer_enterprise:
                hits = self.store.db.execute(
                    'SELECT DISTINCT enterprise, period, part, edition FROM disclosures'
                    ' WHERE valid=1 AND enterprise=? AND part=? AND edition=?',
                    (signer_enterprise, c['target_part'],
                     c['replacement_of'])).fetchall()
                if len(hits) == 1:
                    resolved = hits[0]
            evidence = self._evidence(c['raw_sha256'], c['depth'], None)
            if resolved is None:
                # 无法落地的更正仍必须留待核实线索。签发企业已知时挂到该企业
                # 该册别（期间未知则摊到已知期间，否则用“未知期间”保底）；
                # 签发者不在通讯录则单立保底册别，绝不冒充任何企业的事实。
                entry = {
                    'reason': 'correction_unresolved',
                    'detail': f'replacement-of={c["replacement_of"]} '
                              f'target-part={c["target_part"]} 无法唯一落地'
                              f'（签发者 {c["signer"] or "（空）"}）',
                    'edition': c['replacement_of'], 'evidence': evidence,
                }
                if signer_enterprise:
                    periods = [r['period'] for r in self.store.db.execute(
                        'SELECT DISTINCT period FROM disclosures'
                        ' WHERE valid=1 AND enterprise=?',
                        (signer_enterprise,)).fetchall()]
                    target_periods = periods or ['未知期间']
                    for period in target_periods:
                        bucket(signer_enterprise, period,
                               c['target_part']).pending.append(entry)
                else:
                    # 来源不明的更正：向各通讯录企业的同名册别广播待核实提示，
                    # 不改变任何事实状态；没有已知企业时再用保底册别留痕。
                    known = [
                        (ent, r['period'])
                        for ent in self.store.contact_enterprises()
                        for r in self.store.db.execute(
                            'SELECT DISTINCT period FROM disclosures'
                            ' WHERE valid=1 AND enterprise=?', (ent,)).fetchall()
                    ]
                    if known:
                        for ent, period in known:
                            bucket(ent, period,
                                   c['target_part']).pending.append(entry)
                    else:
                        bucket('（签发者不在通讯录）', '未知期间',
                               c['target_part']).pending.append(entry)
                continue
            key = (resolved['enterprise'], resolved['period'], resolved['part'])
            withdrawals.setdefault(key, []).append({
                'edition': resolved['edition'], 'signer': c['signer'],
                'evidence': evidence,
            })

        # 3) 汇总每个册别状态。待核实是册别下的独立线索，不覆盖
        # “已确认撤回”这一既成事实 —— 两者必须保持可区分。
        manual = self._manual_index()
        for key, ps in list(statuses.items()):
            editions = valid_editions.get(key, [])
            withdrawn_editions = {w['edition'] for w in withdrawals.get(key, [])}
            ps.withdrawals = withdrawals.get(key, [])
            surviving = [e for e in editions
                         if e['edition'] not in withdrawn_editions]
            # 局部替换只撤 target-part；这里按册别独立计算，其他册别天然不受影响
            ps.editions = surviving
            decision = self._matching_manual(manual, key)
            if surviving:
                ps.status = 'available'
            elif editions and not surviving:
                ps.status = 'withdrawn'
            elif ps.pending:
                ps.status = 'pending'
            else:
                ps.status = 'missing'
            self._apply_manual(ps, decision, withdrawn_editions)
        return statuses

    def _manual_index(self) -> list[dict]:
        return [dict(r) for r in self.store.manual_decisions()]

    def _matching_manual(self, manual: list[dict],
                         key: tuple[str, str, str]) -> dict | None:
        enterprise, period, part = key
        for d in manual:
            if d['enterprise'] != enterprise or d['part'] != part:
                continue
            if d['period'] is not None and d['period'] != period:
                continue
            if d['scope_part'] is not None and d['scope_part'] != part:
                continue
            return d
        return None

    def _apply_manual(self, ps: PartStatus, decision: dict | None,
                      withdrawn_editions: set[str]) -> None:
        """人工确认有适用范围；后到事实与之冲突时不冲掉确认，转待核实。"""
        if not decision:
            return
        if decision['kind'] == 'confirm':
            if ps.status == 'available':
                return  # 与确认一致
            ps.pending.append({
                'reason': 'manual_conflict',
                'detail': f'人工确认可用（{decision["rationale"]}），'
                          f'但当前事实状态为 {ps.status}；来信未自动冲掉确认',
                'edition': None,
                'evidence': {'manual_decision_id': decision['id']},
            })
            ps.status = 'pending'
        elif decision['kind'] == 'exclude' and ps.status == 'available':
            ps.pending.append({
                'reason': 'manual_conflict',
                'detail': f'人工排除（{decision["rationale"]}）与现有可用版本冲突，'
                          f'保留待人工处理，未自动改写',
                'edition': None,
                'evidence': {'manual_decision_id': decision['id']},
            })
            ps.status = 'pending'

    def _evidence(self, raw_sha256: str, depth: int | None,
                  part_index: int | None) -> dict:
        """每项判断回到原信与具体 MIME 部分的定位信息。"""
        rows = self.store.db.execute(
            'SELECT folder, generation, uid FROM observations'
            ' WHERE raw_sha256=? ORDER BY id', (raw_sha256,)).fetchall()
        locs = [f'{r["folder"]}@gen{r["generation"]}#uid{r["uid"]}' for r in rows]
        return {
            'raw_sha256': raw_sha256,
            'observed_at': locs,
            'mime_depth': depth,
            'mime_part_index': part_index,
        }
