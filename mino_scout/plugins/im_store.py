"""Scout 本地 IM 消息库 `scout.db`（标准库 sqlite3）。

这是 Scout 第一个本地库，只存 IM 正文和 Nexus 不可达期间的入站暂存。
正文归 Scout、Nexus 不存正文（设计 §7.3）。日志里**绝不**打印正文，只打长度 / 渠道 / ID。

CLAUDE.md 硬约束 1 禁止 `import sqlite3`；本模块是经设计文档 §7.3 / §8.2 明确要求的唯一例外，
只允许本文件使用 sqlite3。

保留期：`config.json` 的 `im_retention_days`，默认 180；0 表示不清理。
启动（首次打开）时清一次，之后每 24 小时清一次（由读写调用顺带触发，不另起线程）。
"""
from __future__ import annotations

import json
import os
import sqlite3  # 设计 §7.3 要求 Scout 本地 scout.db；仅 im_store.py 使用
import threading
import time
from pathlib import Path
from typing import Any

from mino_scout.config import config_dir, load_config
from mino_scout.log import SLog

TAG = "ImStore"
DEFAULT_RETENTION_DAYS = 180
PURGE_INTERVAL_SEC = 24 * 3600
RECENT_TEXT_MAX = 500
STORE_TEXT_MAX = 4000
HISTORY_MAX = 100
PENDING_MAX = 1000

_SCHEMA = """
CREATE TABLE IF NOT EXISTS im_message(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  channel TEXT NOT NULL,
  chat_id TEXT NOT NULL,
  sender_id TEXT NOT NULL DEFAULT '',
  msg_id TEXT NOT NULL,
  role TEXT NOT NULL CHECK(role IN ('user','assistant')),
  text TEXT NOT NULL DEFAULT '',
  ts INTEGER NOT NULL,
  UNIQUE(channel, msg_id, role)
);
CREATE INDEX IF NOT EXISTS idx_im_message_chat ON im_message(channel, chat_id, ts, id);
CREATE INDEX IF NOT EXISTS idx_im_message_ts ON im_message(ts);
CREATE TABLE IF NOT EXISTS im_pending(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  payload_json TEXT NOT NULL,
  ts INTEGER NOT NULL
);
"""


def db_path() -> Path:
    return config_dir() / "scout.db"


def retention_days() -> int:
    raw = load_config().get("im_retention_days", DEFAULT_RETENTION_DAYS)
    try:
        days = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_RETENTION_DAYS
    return days if days >= 0 else DEFAULT_RETENTION_DAYS


class _Store:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.RLock()
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False, timeout=10)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.executescript(_SCHEMA)
        self._db.commit()
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        self._last_purge = 0.0

    def maybe_purge(self, *, force: bool = False) -> int:
        now = time.time()
        if not force and now - self._last_purge < PURGE_INTERVAL_SEC:
            return 0
        self._last_purge = now
        days = retention_days()
        if days <= 0:
            return 0
        return self.purge(days)

    def purge(self, older_than_days: int) -> int:
        cutoff = int(time.time()) - int(older_than_days) * 86400
        with self._lock:
            cur = self._db.execute("DELETE FROM im_message WHERE ts < ?", (cutoff,))
            self._db.commit()
            n = cur.rowcount or 0
        if n:
            SLog.i(TAG, f"purged {n} messages older than {older_than_days}d")
        return n

    def record(self, channel, chat_id, sender_id, msg_id, role, text, ts) -> bool:
        if role not in ("user", "assistant"):
            raise ValueError("role must be user or assistant")
        body = str(text or "")[:STORE_TEXT_MAX]
        with self._lock:
            cur = self._db.execute(
                "INSERT OR IGNORE INTO im_message(channel,chat_id,sender_id,msg_id,role,text,ts)"
                " VALUES(?,?,?,?,?,?,?)",
                (str(channel), str(chat_id), str(sender_id or ""), str(msg_id), role, body,
                 int(ts if ts else time.time())),
            )
            self._db.commit()
            inserted = (cur.rowcount or 0) > 0
        SLog.d(TAG, f"record channel={channel} msg_id={msg_id} role={role} len={len(body)} new={inserted}")
        self.maybe_purge()
        return inserted

    def _rows(self, channel, chat_id, limit, before_ts, cut) -> list[dict[str, Any]]:
        sql = "SELECT role,text,ts FROM im_message WHERE channel=? AND chat_id=?"
        args: list[Any] = [str(channel), str(chat_id)]
        if before_ts is not None:
            sql += " AND ts < ?"
            args.append(int(before_ts))
        sql += " ORDER BY ts DESC, id DESC LIMIT ?"
        args.append(int(limit))
        with self._lock:
            rows = self._db.execute(sql, args).fetchall()
        rows.reverse()
        return [{"role": r[0], "text": r[1][:cut] if cut else r[1], "ts": int(r[2])} for r in rows]

    def recent(self, channel, chat_id, limit=8, before_ts=None):
        self.maybe_purge()
        return self._rows(channel, chat_id, max(0, int(limit)), before_ts, RECENT_TEXT_MAX)

    def history(self, channel, chat_id, limit):
        self.maybe_purge()
        n = max(1, min(int(limit or 20), HISTORY_MAX))
        return self._rows(channel, chat_id, n, None, 0)

    def push(self, payload: dict[str, Any], ts: int | None = None) -> int:
        blob = json.dumps(payload, ensure_ascii=False)
        with self._lock:
            cur = self._db.execute(
                "INSERT INTO im_pending(payload_json,ts) VALUES(?,?)",
                (blob, int(ts if ts else time.time())),
            )
            extra = self._db.execute("SELECT COUNT(*) FROM im_pending").fetchone()[0] - PENDING_MAX
            if extra > 0:
                self._db.execute(
                    "DELETE FROM im_pending WHERE id IN (SELECT id FROM im_pending ORDER BY ts, id LIMIT ?)",
                    (extra,),
                )
            self._db.commit()
            return int(cur.lastrowid or 0)

    def pop_all(self) -> list[tuple[int, dict[str, Any]]]:
        """原子取出并清空，按 (ts, id) 升序。返回 [(ts, payload)]。"""
        with self._lock:
            rows = self._db.execute(
                "SELECT payload_json,ts FROM im_pending ORDER BY ts, id"
            ).fetchall()
            self._db.execute("DELETE FROM im_pending")
            self._db.commit()
        out = []
        for blob, ts in rows:
            try:
                out.append((int(ts), json.loads(blob)))
            except ValueError:
                continue
        return out

    def pending_count(self) -> int:
        with self._lock:
            return int(self._db.execute("SELECT COUNT(*) FROM im_pending").fetchone()[0])

    def close(self) -> None:
        with self._lock:
            self._db.close()


_store: _Store | None = None
_store_lock = threading.Lock()


def _get() -> _Store:
    """路径跟随 config_dir()（可用 MINO_SCOUT_HOME 指到别处）。路径变了就重开。"""
    global _store
    path = db_path()
    with _store_lock:
        if _store is None or _store.path != path:
            if _store is not None:
                _store.close()
            _store = _Store(path)
            _store.maybe_purge(force=True)
        return _store


def record(channel: str, chat_id: str, sender_id: str, msg_id: str, role: str, text: str, ts: int | None = None) -> bool:
    """写一条。(channel,msg_id,role) 重复则忽略，返回 False。"""
    return _get().record(channel, chat_id, sender_id, msg_id, role, text, ts)


def recent(channel: str, chat_id: str, limit: int = 8, before_ts: int | None = None) -> list[dict[str, Any]]:
    return _get().recent(channel, chat_id, limit, before_ts)


def history(channel: str, chat_id: str, limit: int = 20) -> list[dict[str, Any]]:
    return _get().history(channel, chat_id, limit)


def purge(older_than_days: int) -> int:
    return _get().purge(older_than_days)


def push(payload: dict[str, Any], ts: int | None = None) -> int:
    return _get().push(payload, ts)


def pop_all() -> list[tuple[int, dict[str, Any]]]:
    return _get().pop_all()


def pending_count() -> int:
    return _get().pending_count()
