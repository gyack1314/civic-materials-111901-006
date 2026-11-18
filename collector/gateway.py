"""授权邮箱网关 HTTP 客户端，只访问网关基址，不访问邮件中的任何链接。"""
from __future__ import annotations

import http.client
import json
from dataclasses import dataclass
from urllib.parse import urlsplit


class GenerationConflict(RuntimeError):
    """文件夹代际已改变，本页结果必须丢弃并按新代际重新枚举。"""

    def __init__(self, folder: str, current_generation: int):
        super().__init__(f'{folder} 代际冲突，当前代际 {current_generation}')
        self.folder = folder
        self.current_generation = current_generation


class GatewayError(RuntimeError):
    pass


@dataclass(frozen=True)
class Folder:
    name: str
    generation: int


@dataclass(frozen=True)
class MessageRef:
    uid: int
    raw_path: str
    message_id: str
    folder: str


@dataclass(frozen=True)
class MailboxPage:
    generation: int
    through: int
    messages: tuple[MessageRef, ...]


class Gateway:
    def __init__(self, base_url: str, timeout: float = 30.0):
        parts = urlsplit(base_url)
        if parts.scheme != 'http':
            raise GatewayError('仅支持 http 网关: ' + base_url)
        self._host = parts.hostname
        self._port = parts.port or 80
        self._prefix = parts.path.rstrip('/')
        self._timeout = timeout

    def _request(self, method: str, path: str):
        conn = http.client.HTTPConnection(self._host, self._port, timeout=self._timeout)
        try:
            conn.request(method, self._prefix + path)
            resp = conn.getresponse()
            data = resp.read()
        except (OSError, http.client.HTTPException) as exc:
            raise GatewayError(f'网关请求失败 {path}: {exc}') from exc
        finally:
            conn.close()
        if resp.status == 409:
            try:
                payload = json.loads(data.decode('utf-8'))
            except (ValueError, UnicodeDecodeError):
                payload = {}
            folder = path.split('folder=', 1)[-1].split('&', 1)[0]
            raise GenerationConflict(folder, int(payload.get('generation', -1)))
        if resp.status >= 400:
            raise GatewayError(f'网关返回 {resp.status}: {data[:200]!r}')
        try:
            return json.loads(data.decode('utf-8'))
        except (ValueError, UnicodeDecodeError) as exc:
            raise GatewayError('网关响应不是 JSON') from exc

    def folders(self) -> tuple[Folder, ...]:
        payload = self._request('GET', '/folders')
        return tuple(Folder(f['name'], int(f['generation'])) for f in payload['folders'])

    def mailbox(self, folder: str, generation: int, after: int) -> MailboxPage:
        payload = self._request(
            'GET', f'/mailbox?folder={folder}&generation={generation}&after={after}')
        messages = tuple(
            MessageRef(int(m['uid']), m['raw'], m['message_id'], m['folder'])
            for m in payload['messages']
        )
        return MailboxPage(int(payload['generation']), int(payload['through']), messages)

    def raw(self, raw_path: str) -> bytes:
        # raw_path 来自网关清单；只允许同源 /raw/ 路径
        if not raw_path.startswith('/raw/'):
            raise GatewayError('拒绝非 /raw/ 来源路径: ' + raw_path)
        conn = http.client.HTTPConnection(self._host, self._port, timeout=self._timeout)
        try:
            conn.request('GET', raw_path)
            resp = conn.getresponse()
            data = resp.read()
        except (OSError, http.client.HTTPException) as exc:
            raise GatewayError(f'原信下载失败 {raw_path}: {exc}') from exc
        finally:
            conn.close()
        if resp.status >= 400:
            raise GatewayError(f'原信下载返回 {resp.status}')
        return data

    def contacts(self) -> dict[str, tuple[str, ...]]:
        payload = self._request('GET', '/contacts')
        return {enterprise: tuple(addr.lower() for addr in addrs)
                for enterprise, addrs in payload.items()}
