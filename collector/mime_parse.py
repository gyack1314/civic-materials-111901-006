"""MIME 原信解析：只解析结构与字节，不执行、不渲染任何内容。

输出扁平的部分列表，嵌套 message/rfc822 递归进入；每个叶子部分保留自己的
原始字节，证据链可回到整封原信中的 depth/index 路径。
"""
from __future__ import annotations

from dataclasses import dataclass
from email.parser import BytesParser
from email.policy import default as default_policy
from email.utils import parseaddr


@dataclass(frozen=True)
class MimePart:
    depth: int
    index: int
    content_type: str
    filename: str | None
    disposition: str | None
    cid: str | None
    blob: bytes
    # message/rfc822 层携带的内层信头；普通部分为 None
    envelope_from: str | None = None
    envelope_message_id: str | None = None
    is_container: bool = False


@dataclass(frozen=True)
class ParsedMessage:
    message_id: str
    sender: str
    subject: str
    parts: tuple[MimePart, ...]


def _addr_only(value: str | None) -> str:
    # 只读地址部分，显示名不参与身份判断
    return parseaddr(value or '')[1].lower()


def parse_message(raw: bytes) -> ParsedMessage:
    """把一封原信解析成扁平部分列表。原始字节不由本函数修改。"""
    root = BytesParser(policy=default_policy).parsebytes(raw)
    parts: list[MimePart] = []
    root_sender = _addr_only(root.get('From'))
    root_mid = (root.get('Message-ID') or '').strip()

    def walk(msg, depth: int, index: int,
             carrier_sender: str, carrier_mid: str) -> None:
        # 注意：message/rfc822 同时是 multipart，必须在 is_multipart() 之前判断，
        # 否则嵌套原信的容器层会被跳过，内层部分会错挂外层转发者身份。
        if msg.get_content_type() == 'message/rfc822':
            inner = msg.get_payload(0)
            inner_sender = _addr_only(inner.get('From'))
            inner_mid = (inner.get('Message-ID') or '').strip()
            parts.append(MimePart(
                depth=depth, index=index, content_type='message/rfc822',
                filename=msg.get_filename(),
                disposition=msg.get_content_disposition(), cid=msg.get('Content-ID'),
                blob=msg.get_payload(decode=True) or b'',
                envelope_from=inner_sender,
                envelope_message_id=inner_mid,
                is_container=True,
            ))
            walk(inner, depth + 1, index, inner_sender, inner_mid)
            return
        if msg.is_multipart():
            for i, child in enumerate(msg.iter_parts()):
                walk(child, depth + 1, i, carrier_sender, carrier_mid)
            return
        content_type = msg.get_content_type()
        disposition = msg.get_content_disposition()
        filename = msg.get_filename()  # policy 已处理 RFC2231/编码字
        cid = msg.get('Content-ID')
        blob = msg.get_payload(decode=True)
        if blob is None:
            blob = b''
        parts.append(MimePart(
            depth=depth, index=index, content_type=content_type,
            filename=filename, disposition=disposition,
            cid=cid.strip() if cid else None, blob=blob,
            envelope_from=carrier_sender, envelope_message_id=carrier_mid,
        ))
    walk(root, 0, -1, root_sender, root_mid)
    return ParsedMessage(
        message_id=root_mid, sender=root_sender,
        subject=root.get('Subject') or '',
        parts=tuple(parts),
    )


def text_bodies(parsed: ParsedMessage) -> tuple[tuple[int, str], ...]:
    """返回 (depth, 纯文本正文) 列表。HTML 不渲染、不去链，只按文本留证。"""
    out = []
    for part in parsed.parts:
        if part.content_type == 'text/plain' and not part.is_container:
            try:
                out.append((part.depth, part.blob.decode('utf-8')))
            except UnicodeDecodeError:
                out.append((part.depth, part.blob.decode('utf-8', errors='replace')))
    return tuple(out)
