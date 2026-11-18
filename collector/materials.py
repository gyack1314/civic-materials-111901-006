"""合成披露文件与更正正文的解析。

披露文件是 UTF-8 的 key=value 文本，必须同时含 enterprise/period/part/edition
四个键且各只出现一次；任何缺键、重复键、无法解码或无法识别的行都判为无效，
调用方据此挂待核实，而不是猜测或忽略。
"""
from __future__ import annotations

from dataclasses import dataclass

REQUIRED_KEYS = ('enterprise', 'period', 'part', 'edition')


@dataclass(frozen=True)
class Disclosure:
    enterprise: str
    period: str
    part: str
    edition: str


@dataclass(frozen=True)
class DisclosureParseResult:
    valid: bool
    error: str | None
    disclosure: Disclosure | None


def parse_disclosure(blob: bytes) -> DisclosureParseResult:
    try:
        text = blob.decode('utf-8')
    except UnicodeDecodeError as exc:
        return DisclosureParseResult(False, f'文件不是 UTF-8 文本: {exc}', None)
    values: dict[str, str] = {}
    for lineno, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue
        if '=' not in line:
            return DisclosureParseResult(False, f'第 {lineno} 行不是 key=value', None)
        key, value = line.split('=', 1)
        key, value = key.strip(), value.strip()
        if not key or not value:
            return DisclosureParseResult(False, f'第 {lineno} 行键或值为空', None)
        if key in values:
            return DisclosureParseResult(False, f'键 {key} 重复出现', None)
        values[key] = value
    # 未知字段（含字段名拼错，如 edion）先判：不能让拼写错误悄悄绕过校验
    unknown = sorted(set(values) - set(REQUIRED_KEYS))
    if unknown:
        return DisclosureParseResult(False, '出现未知字段: ' + ','.join(unknown), None)
    missing = [k for k in REQUIRED_KEYS if k not in values]
    if missing:
        return DisclosureParseResult(False, '缺少必需字段: ' + ','.join(missing), None)
    return DisclosureParseResult(
        True, None,
        Disclosure(values['enterprise'], values['period'],
                   values['part'], values['edition']),
    )


@dataclass(frozen=True)
class CorrectionInstruction:
    replacement_of: str
    target_part: str


def parse_correction(body: str) -> CorrectionInstruction | None:
    """只采信结构化的 replacement-of / target-part 两行；缺一即不构成更正事实。"""
    found: dict[str, str] = {}
    for raw_line in body.splitlines():
        line = raw_line.strip()
        if '=' not in line:
            continue
        key, value = line.split('=', 1)
        key, value = key.strip(), value.strip()
        if key in ('replacement-of', 'target-part') and value:
            if key in found and found[key] != value:
                # 同名键自相矛盾，声明不清不构成确认事实
                return None
            found[key] = value
    if set(found) == {'replacement-of', 'target-part'}:
        return CorrectionInstruction(found['replacement-of'], found['target-part'])
    return None
