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
import hashlib
import os
import sqlite3  # 设计 §7.3 要求 Scout 本地 scout.db；仅 im_store.py 使用
import threading
import time
from pathlib import Path
from typing import Any

from mino_scout.config import load_config
from mino_scout.plugins.paths import data_dir
from mino_scout.tools.keep_alive.log import SLog

TAG = "ImStore"
DEFAULT_RETENTION_DAYS = 180
PURGE_INTERVAL_SEC = 24 * 3600
RECENT_TEXT_MAX = 500
STORE_TEXT_MAX = 4000
HISTORY_MAX = 100
PENDING_MAX = 1000

_SCHEMA = """
CREATE TABLE IF NOT EXISTS im_stream(
  key TEXT PRIMARY KEY, scope TEXT NOT NULL, provider_message_id TEXT NOT NULL DEFAULT '',
  sequence INTEGER NOT NULL DEFAULT 0, completed INTEGER NOT NULL DEFAULT 0, ts INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS im_attachment(
  ref TEXT PRIMARY KEY, scope TEXT NOT NULL, data BLOB NOT NULL, mime TEXT NOT NULL, ts INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS im_inbox(
  key TEXT PRIMARY KEY, scope TEXT NOT NULL, payload_json TEXT NOT NULL,
  ts INTEGER NOT NULL, next_send INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS im_outbox(
  key TEXT PRIMARY KEY, scope TEXT NOT NULL, text TEXT NOT NULL, card_json TEXT NOT NULL DEFAULT '{}',
  state TEXT NOT NULL, provider_message_id TEXT NOT NULL DEFAULT '', ts INTEGER NOT NULL
);
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
    return data_dir() / "scout.db"


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
        columns = {r[1] for r in self._db.execute('PRAGMA table_info(im_message)')}
        if 'thread_id' not in columns:
            self._db.execute("ALTER TABLE im_message ADD COLUMN thread_id TEXT NOT NULL DEFAULT ''")
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
            self._db.execute("DELETE FROM im_attachment WHERE ts < ?", (cutoff,))
            self._db.execute("DELETE FROM im_outbox WHERE ts < ? AND state='done'", (cutoff,))
            self._db.execute("DELETE FROM im_stream WHERE ts < ? AND completed=1", (cutoff,))
            self._db.commit()
            n = cur.rowcount or 0
        if n:
            SLog.i(TAG, f"purged {n} messages older than {older_than_days}d")
        return n

    def record(self, channel, chat_id, sender_id, msg_id, role, text, ts, thread_id='') -> bool:
        if role not in ("user", "assistant"):
            raise ValueError("role must be user or assistant")
        body = str(text or "")[:STORE_TEXT_MAX]
        with self._lock:
            cur = self._db.execute(
                "INSERT OR IGNORE INTO im_message(channel,chat_id,sender_id,msg_id,role,text,ts,thread_id)"
                " VALUES(?,?,?,?,?,?,?,?)",
                (str(channel), str(chat_id), str(sender_id or ""), str(msg_id), role, body,
                 int(ts if ts else time.time()), str(thread_id)),
            )
            self._db.commit()
            inserted = (cur.rowcount or 0) > 0
        SLog.d(TAG, f"record channel={channel} msg_id={msg_id} role={role} len={len(body)} new={inserted}")
        self.maybe_purge()
        return inserted

    def _rows(self, channel, chat_id, limit, before_ts, cut, thread_id=None) -> list[dict[str, Any]]:
        sql = "SELECT role,text,ts FROM im_message WHERE channel=? AND chat_id=?"
        args: list[Any] = [str(channel), str(chat_id)]
        if thread_id is not None:
            sql += ' AND thread_id=?'
            args.append(str(thread_id))
        if before_ts is not None:
            sql += " AND ts < ?"
            args.append(int(before_ts))
        sql += " ORDER BY ts DESC, id DESC LIMIT ?"
        args.append(int(limit))
        with self._lock:
            rows = self._db.execute(sql, args).fetchall()
        rows.reverse()
        result = [{"role": r[0], "text": r[1][:cut] if cut else r[1], "ts": int(r[2])} for r in rows]
        if not cut:
            with self._lock:
                cards = self._db.execute("SELECT text, card_json FROM im_outbox WHERE scope=? AND state='done' ORDER BY ts DESC LIMIT ?", (str(channel), int(limit))).fetchall()
            for item in result:
                if item['role'] == 'assistant':
                    match = next((row for row in cards if row[0] == item['text']), None)
                    if match:
                        item['card'] = json.loads(match[1])
        return result

    def recent(self, channel, chat_id, limit=8, before_ts=None, thread_id=''):
        self.maybe_purge()
        return self._rows(channel, chat_id, max(0, int(limit)), before_ts, RECENT_TEXT_MAX, thread_id)

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
    """路径跟随当前账号插件存储（兼容 MINO_SCOUT_HOME）。路径变了就重开。"""
    global _store
    path = db_path()
    with _store_lock:
        if _store is None or _store.path != path:
            if _store is not None:
                _store.close()
            _store = _Store(path)
            _store.maybe_purge(force=True)
        return _store


def _scope(channel: str, instance_id: str, tenant: str, sender_id: str) -> str:
    cfg = load_config()
    owner = str(cfg.get("plugin_owner_user_id") or "")
    origin = str(cfg.get("plugin_owner_nexus") or "")
    if not owner or not origin or not instance_id or not sender_id:
        raise PermissionError("IM 历史缺少已确认的账号、实例或发送者")
    parts = [origin, owner, channel, instance_id, tenant, sender_id]
    return "scoped:" + hashlib.sha256(json.dumps(parts, ensure_ascii=False).encode()).hexdigest()


def record(channel: str, chat_id: str, sender_id: str, msg_id: str, role: str, text: str,
           ts: int | None = None, *, instance_id: str = "", tenant: str = "", thread_id: str = "") -> bool:
    return _get().record(_scope(channel, instance_id, tenant, sender_id), chat_id,
                         sender_id, msg_id, role, text, ts, thread_id)


def _messages(channel: str, chat_id: str, sender_id: str, instance_id: str, tenant: str,
              limit: int, before_ts: int | None = None, *, full: bool = False,
              after_ts: int = 0, thread_id: str | None = None) -> list[dict[str, Any]]:
    scope = _scope(channel, instance_id, tenant, sender_id)
    rows = (_get().history(scope, chat_id, limit) if full
            else _get().recent(scope, chat_id, min(8, max(0, limit)), before_ts, thread_id))
    return [{**r, "sender_id": sender_id, "instance_id": instance_id, "tenant": tenant, "thread_id": str(thread_id or "")}
            for r in rows if int(r["ts"]) > after_ts]


def recent(channel: str, chat_id: str, limit: int = 8, before_ts: int | None = None, *,
           sender_id: str = "", instance_id: str = "", tenant: str = "", thread_id: str = "") -> list[dict[str, Any]]:
    return _messages(channel, chat_id, sender_id, instance_id, tenant, limit, before_ts, thread_id=thread_id)


def history(channel: str, chat_id: str, limit: int = 20, *, sender_id: str = "",
            instance_id: str = "", tenant: str = "", after_ts: int = 0) -> list[dict[str, Any]]:
    return _messages(channel, chat_id, sender_id, instance_id, tenant, limit,
                     full=True, after_ts=after_ts)


def purge(older_than_days: int) -> int:
    return _get().purge(older_than_days)


def push(payload: dict[str, Any], ts: int | None = None) -> int:
    return _get().push(payload, ts)


def pop_all() -> list[tuple[int, dict[str, Any]]]:
    return _get().pop_all()


def pending_count() -> int:
    return _get().pending_count()



def durable_ingest(payload: dict[str, Any], ts: int) -> bool:
    store = _get()
    scope = _scope(payload['channel'], payload.get('instance_id', ''), payload.get('tenant', ''), payload['sender_id'])
    key = hashlib.sha256(json.dumps([scope, payload['chat_id'], payload.get('thread_id', ''), payload['msg_id'],
                                    payload.get('event_type', 'message'), payload.get('revision', '')]).encode()).hexdigest()
    payload['delivery_key'] = key
    with store._lock:
        store._db.execute('BEGIN IMMEDIATE')
        try:
            fresh = store._db.execute('INSERT OR IGNORE INTO im_inbox(key,scope,payload_json,ts,next_send) VALUES(?,?,?,?,0)',
                                     (key, scope, json.dumps(payload, ensure_ascii=False), ts)).rowcount == 1
            # 插件消息正文与待转发事件在同一事务内受理。
            store._db.execute('INSERT OR IGNORE INTO im_message(channel,chat_id,sender_id,msg_id,role,text,ts,thread_id) VALUES(?,?,?,?,?,?,?,?)',
                (scope, payload['chat_id'], payload['sender_id'], payload['msg_id'], 'user', payload.get('text', ''), ts, str(payload.get('thread_id') or '')))
            store._db.commit()
        except Exception:
            store._db.rollback()
            raise
    return fresh


def pending_events() -> list[dict]:
    store = _get()
    with store._lock:
        rows = store._db.execute('SELECT payload_json FROM im_inbox WHERE next_send<=? ORDER BY ts,key LIMIT 100', (int(time.time()),)).fetchall()
    return [json.loads(row[0]) for row in rows]


def sent_event(key: str) -> None:
    store = _get()
    with store._lock:
        store._db.execute('UPDATE im_inbox SET next_send=? WHERE key=?', (int(time.time()) + 60, key))
        store._db.commit()


def acknowledge(channel: str, instance_id: str, tenant: str, sender_id: str, key: str) -> None:
    scope, store = _scope(channel, instance_id, tenant, sender_id), _get()
    with store._lock:
        store._db.execute('DELETE FROM im_inbox WHERE key=? AND scope=?', (key, scope))
        store._db.commit()


def send_once(channel: str, instance_id: str, tenant: str, sender_id: str, key: str, text: str, card: dict, send) -> dict:
    store, scope = _get(), _scope(channel, instance_id, tenant, sender_id)
    with store._lock:
        store._db.execute('BEGIN IMMEDIATE')
        try:
            row = store._db.execute('SELECT scope,state,provider_message_id FROM im_outbox WHERE key=?', (key,)).fetchone()
            if row:
                store._db.commit()
                if row[0] != scope:
                    raise PermissionError('投递不属于当前账号')
                if row[1] == 'done':
                    return {'ok': True, 'provider_message_id': row[2], 'summary': '已发送'}
                raise RuntimeError('上次投递结果未知，不能自动重复发送')
            store._db.execute('INSERT INTO im_outbox(key,scope,text,card_json,state,ts) VALUES(?,?,?,?,?,?)',
                (key, scope, text, json.dumps(card, ensure_ascii=False), 'sending', int(time.time())))
            store._db.commit()
        except Exception:
            store._db.rollback()
            raise
    try:
        sent = send()
        provider_id = str((sent or {}).get('provider_message_id') or '') if isinstance(sent, dict) else ''
    except Exception:
        with store._lock:
            store._db.execute("UPDATE im_outbox SET state='uncertain' WHERE key=?", (key,))
            store._db.commit()
        raise
    with store._lock:
        store._db.execute("UPDATE im_outbox SET state='done',provider_message_id=? WHERE key=?", (provider_id, key))
        store._db.commit()
    return {'ok': True, 'provider_message_id': provider_id, 'summary': '已发送'}



def migrate_pending() -> None:
    store = _get()
    with store._lock:
        rows = store._db.execute('SELECT id,payload_json,ts FROM im_pending ORDER BY ts,id').fetchall()
    for record_id, blob, ts in rows:
        payload = json.loads(blob)
        durable_ingest(payload, ts)
        with store._lock:
            store._db.execute('DELETE FROM im_pending WHERE id=?', (record_id,))
            store._db.commit()



def save_attachment(origin: dict, data: bytes, mime: str) -> str:
    import uuid
    scope = _scope(origin['channel'], origin.get('instance_id', ''), origin.get('tenant', ''), origin['sender_id'])
    ref, store = 'media-' + uuid.uuid4().hex, _get()
    with store._lock:
        store._db.execute('INSERT INTO im_attachment(ref,scope,data,mime,ts) VALUES(?,?,?,?,?)',
                          (ref, scope, data, mime, int(time.time())))
        store._db.commit()
    return ref


def read_attachment(origin: dict, ref: str) -> dict:
    import base64
    scope = _scope(origin['channel'], origin.get('instance_id', ''), origin.get('tenant', ''), origin['sender_id'])
    store = _get()
    with store._lock:
        row = store._db.execute('SELECT data,mime FROM im_attachment WHERE ref=? AND scope=?', (ref, scope)).fetchone()
    if not row:
        raise ValueError('附件已过期或不属于当前账号')
    return {'base64': base64.b64encode(row[0]).decode(), 'mime': row[1]}



def outgoing_reference(origin: dict, text: str) -> str:
    scope, store = _scope(origin['channel'], origin.get('instance_id', ''), origin.get('tenant', ''), origin['sender_id']), _get()
    with store._lock:
        rows = store._db.execute("SELECT key FROM im_outbox WHERE scope=? AND text=? AND state='done' LIMIT 2", (scope, text)).fetchall()
    return str(rows[0][0]) if len(rows) == 1 else ''


_stream_lock = threading.RLock()

def stream_once(origin: dict, key: str, sequence: int, text: str, card: dict, complete: bool, send) -> dict:
    scope = _scope(origin['channel'], origin.get('instance_id', ''), origin.get('tenant', ''), origin['sender_id'])
    store = _get()
    with _stream_lock:
        with store._lock:
            row = store._db.execute('SELECT scope,provider_message_id,sequence,completed FROM im_stream WHERE key=?', (key,)).fetchone()
        if row and row[0] != scope:
            raise PermissionError('流式消息不属于当前账号')
        if row and not row[1]:
            with store._lock:
                previous = store._db.execute('SELECT state,provider_message_id FROM im_outbox WHERE key=? AND scope=?', (key + ':' + str(row[2]), scope)).fetchone()
            if previous and previous[0] == 'done' and previous[1]:
                row = (row[0], previous[1], row[2], row[3])
                with store._lock:
                    store._db.execute('UPDATE im_stream SET provider_message_id=? WHERE key=?', (row[1], key))
                    store._db.commit()
            else:
                raise RuntimeError('上次草稿创建结果未知，不能重复创建')
        if row and (sequence <= row[2] or row[3]):
            return {'ok': True, 'provider_message_id': row[1], 'summary': '已处理此版本'}
        mid = row[1] if row else ''
        if row is None:
            with store._lock:
                store._db.execute('INSERT INTO im_stream(key,scope,provider_message_id,sequence,completed,ts) VALUES(?,?,?, ?,0,?)', (key, scope, '', sequence, int(time.time())))
                store._db.commit()
        receipt = send_once(origin['channel'], origin.get('instance_id', ''), origin.get('tenant', ''), origin['sender_id'],
                            key + ':' + str(sequence), text, card, lambda: send(mid))
        with store._lock:
            store._db.execute('INSERT INTO im_stream(key,scope,provider_message_id,sequence,completed,ts) VALUES(?,?,?,?,?,?) '
                              'ON CONFLICT(key) DO UPDATE SET provider_message_id=excluded.provider_message_id,sequence=excluded.sequence,completed=excluded.completed,ts=excluded.ts',
                              (key, scope, receipt['provider_message_id'], sequence, int(complete), int(time.time())))
            store._db.commit()
        return receipt
