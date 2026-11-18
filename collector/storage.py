"""本地状态：内容寻址 blob 存储 + 只追加 SQLite 台账。

台账只记事实与人工决定，状态投影在 engine 中计算；同一原信重复写入是幂等的。
"""
from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS raw_messages(
    sha256 TEXT PRIMARY KEY,
    message_id TEXT NOT NULL,
    bytes INTEGER NOT NULL,
    observed_first_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS observations(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    raw_sha256 TEXT NOT NULL REFERENCES raw_messages(sha256),
    folder TEXT NOT NULL,
    generation INTEGER NOT NULL,
    uid INTEGER NOT NULL,
    observed_at TEXT NOT NULL,
    UNIQUE(folder, generation, uid)
);
CREATE TABLE IF NOT EXISTS mime_parts(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    raw_sha256 TEXT NOT NULL REFERENCES raw_messages(sha256),
    depth INTEGER NOT NULL,
    part_index INTEGER NOT NULL,
    content_type TEXT NOT NULL,
    filename TEXT,
    disposition TEXT,
    cid TEXT,
    blob_sha256 TEXT NOT NULL,
    envelope_from TEXT,
    is_container INTEGER NOT NULL DEFAULT 0,
    UNIQUE(raw_sha256, depth, part_index)
);
CREATE TABLE IF NOT EXISTS disclosures(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    part_id INTEGER NOT NULL REFERENCES mime_parts(id),
    raw_sha256 TEXT NOT NULL,
    depth INTEGER NOT NULL,
    part_index INTEGER NOT NULL,
    valid INTEGER NOT NULL,
    parse_error TEXT,
    enterprise TEXT,
    period TEXT,
    part TEXT,
    edition TEXT,
    blob_sha256 TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS corrections(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    raw_sha256 TEXT NOT NULL,
    depth INTEGER NOT NULL,
    signer TEXT NOT NULL,
    replacement_of TEXT NOT NULL,
    target_part TEXT NOT NULL,
    body_excerpt TEXT NOT NULL,
    UNIQUE(raw_sha256, depth)
);
CREATE TABLE IF NOT EXISTS manual_decisions(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    enterprise TEXT NOT NULL,
    period TEXT,
    part TEXT NOT NULL,
    scope_part TEXT,
    kind TEXT NOT NULL CHECK(kind IN ('confirm','exclude')),
    rationale TEXT NOT NULL,
    created_at TEXT NOT NULL,
    superseded_by INTEGER REFERENCES manual_decisions(id)
);
CREATE TABLE IF NOT EXISTS checkpoints(
    folder TEXT PRIMARY KEY,
    generation INTEGER NOT NULL,
    through INTEGER NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS contacts(
    enterprise TEXT NOT NULL,
    address TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(enterprise, address)
);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


class Store:
    def __init__(self, state_dir: str | Path):
        self.root = Path(state_dir)
        self.blobs = self.root / 'blobs'
        self.blobs.mkdir(parents=True, exist_ok=True)
        self.db_path = self.root / 'ledger.sqlite3'
        self.db = sqlite3.connect(self.db_path)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA foreign_keys=ON')
        self.db.executescript(SCHEMA)
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    def __enter__(self) -> 'Store':
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def put_blob(self, data: bytes) -> str:
        digest = hashlib.sha256(data).hexdigest()
        path = self.blobs / digest[:2] / digest
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix('.tmp')
            tmp.write_bytes(data)
            tmp.replace(path)
        return digest

    def blob_path(self, digest: str) -> Path:
        return self.blobs / digest[:2] / digest

    def raw_exists(self, digest: str) -> bool:
        row = self.db.execute(
            'SELECT 1 FROM raw_messages WHERE sha256=?', (digest,)).fetchone()
        return row is not None

    # ---- 只追加写入 ----------------------------------------------------

    def add_raw(self, digest: str, message_id: str, size: int) -> None:
        self.db.execute(
            'INSERT OR IGNORE INTO raw_messages VALUES(?,?,?,?)',
            (digest, message_id, size, utcnow()))

    def add_observation(self, digest: str, folder: str, generation: int,
                        uid: int) -> None:
        self.db.execute(
            'INSERT OR IGNORE INTO observations'
            '(raw_sha256, folder, generation, uid, observed_at) VALUES(?,?,?,?,?)',
            (digest, folder, generation, uid, utcnow()))

    def add_part(self, digest: str, depth: int, index: int, content_type: str,
                 filename: str | None, disposition: str | None, cid: str | None,
                 blob_digest: str, envelope_from: str | None,
                 is_container: bool) -> int:
        cur = self.db.execute(
            'INSERT OR IGNORE INTO mime_parts'
            '(raw_sha256, depth, part_index, content_type, filename, disposition,'
            ' cid, blob_sha256, envelope_from, is_container) VALUES(?,?,?,?,?,?,?,?,?,?)',
            (digest, depth, index, content_type, filename, disposition, cid,
             blob_digest, envelope_from, 1 if is_container else 0))
        row = self.db.execute(
            'SELECT id FROM mime_parts WHERE raw_sha256=? AND depth=? AND part_index=?',
            (digest, depth, index)).fetchone()
        return int(row['id'])

    def add_disclosure(self, part_id: int, digest: str, depth: int, index: int,
                       valid: bool, error: str | None, fields: dict | None,
                       blob_digest: str) -> None:
        self.db.execute(
            'INSERT INTO disclosures'
            '(part_id, raw_sha256, depth, part_index, valid, parse_error,'
            ' enterprise, period, part, edition, blob_sha256)'
            ' VALUES(?,?,?,?,?,?,?,?,?,?,?)',
            (part_id, digest, depth, index, 1 if valid else 0, error,
             fields.get('enterprise') if fields else None,
             fields.get('period') if fields else None,
             fields.get('part') if fields else None,
             fields.get('edition') if fields else None,
             blob_digest))

    def add_correction(self, digest: str, depth: int, signer: str,
                       replacement_of: str, target_part: str,
                       excerpt: str) -> None:
        self.db.execute(
            'INSERT OR IGNORE INTO corrections'
            '(raw_sha256, depth, signer, replacement_of, target_part, body_excerpt)'
            ' VALUES(?,?,?,?,?,?)',
            (digest, depth, signer, replacement_of, target_part, excerpt))

    def replace_contacts(self, contacts: dict[str, tuple[str, ...]]) -> None:
        with self.db:
            self.db.execute('DELETE FROM contacts')
            for enterprise, addresses in contacts.items():
                for address in addresses:
                    self.db.execute('INSERT INTO contacts VALUES(?,?,?)',
                                    (enterprise, address, utcnow()))

    def contact_enterprises(self) -> dict[str, tuple[str, ...]]:
        out: dict[str, list[str]] = {}
        for row in self.db.execute(
                'SELECT enterprise, address FROM contacts ORDER BY enterprise, address'):
            out.setdefault(row['enterprise'], []).append(row['address'])
        return {k: tuple(v) for k, v in out.items()}

    # ---- 检查点 --------------------------------------------------------

    def checkpoint(self, folder: str) -> sqlite3.Row | None:
        return self.db.execute(
            'SELECT * FROM checkpoints WHERE folder=?', (folder,)).fetchone()

    def save_checkpoint(self, folder: str, generation: int, through: int) -> None:
        self.db.execute(
            'INSERT INTO checkpoints(folder, generation, through, updated_at)'
            ' VALUES(?,?,?,?) ON CONFLICT(folder) DO UPDATE SET'
            ' generation=excluded.generation, through=excluded.through,'
            ' updated_at=excluded.updated_at',
            (folder, generation, through, utcnow()))

    # ---- 人工决定 ------------------------------------------------------

    def add_manual_decision(self, enterprise: str, period: str | None,
                            part: str, scope_part: str | None, kind: str,
                            rationale: str) -> int:
        cur = self.db.execute(
            'INSERT INTO manual_decisions'
            '(enterprise, period, part, scope_part, kind, rationale, created_at)'
            ' VALUES(?,?,?,?,?,?,?)',
            (enterprise, period, part, scope_part, kind, rationale, utcnow()))
        return int(cur.lastrowid)

    def manual_decisions(self) -> tuple[sqlite3.Row, ...]:
        return tuple(self.db.execute(
            'SELECT * FROM manual_decisions WHERE superseded_by IS NULL'
            ' ORDER BY id').fetchall())

    def commit(self) -> None:
        self.db.commit()
