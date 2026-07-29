#!/usr/bin/env python3
"""
SQLite-backed durable store for IAC Bus coordination MVP.

Persists messages (incl. queue leases), agent registry/heartbeats, and
repo/path resource locks. Thread-safe via an internal RLock.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

ORDINAL_RE = re.compile(r"^([0-9]+)(-[0-9]+)*$")
VALID_MEDIA = frozenset({"web", "slack", "ide", "api", "automation", "other"})
VALID_LOCK_MODES = frozenset({"exclusive", "shared"})

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS bus_messages (
    id TEXT PRIMARY KEY,
    ts REAL NOT NULL,
    protocol TEXT,
    channel TEXT NOT NULL,
    sender TEXT NOT NULL,
    message TEXT NOT NULL,
    type TEXT,
    queue TEXT NOT NULL DEFAULT '',
    priority INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL,
    leased_by TEXT NOT NULL DEFAULT '',
    lease_until REAL NOT NULL DEFAULT 0,
    lease_id TEXT NOT NULL DEFAULT '',
    conversation_id TEXT,
    reply_to TEXT,
    recipient TEXT,
    "group" TEXT,
    headers TEXT,
    ttl_seconds REAL,
    seq INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_bus_messages_seq ON bus_messages(seq);
CREATE INDEX IF NOT EXISTS idx_bus_messages_channel_seq ON bus_messages(channel, seq);
CREATE INDEX IF NOT EXISTS idx_bus_messages_queue_status ON bus_messages(queue, status);

CREATE TABLE IF NOT EXISTS agents (
    agent_uuid TEXT PRIMARY KEY,
    brand TEXT NOT NULL,
    repo_locale TEXT NOT NULL,
    ordinal_path TEXT NOT NULL,
    logical_handle TEXT NOT NULL UNIQUE,
    parent_agent_uuid TEXT,
    role TEXT NOT NULL DEFAULT 'worker',
    status TEXT NOT NULL DEFAULT 'active',
    metadata TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    last_seen_at REAL
);

CREATE TABLE IF NOT EXISTS agent_endpoints (
    endpoint_uuid TEXT PRIMARY KEY,
    agent_uuid TEXT NOT NULL REFERENCES agents(agent_uuid),
    medium TEXT NOT NULL,
    endpoint_handle TEXT NOT NULL UNIQUE,
    session_id TEXT,
    is_active INTEGER NOT NULL DEFAULT 1,
    last_seen_at REAL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    UNIQUE (agent_uuid, medium, session_id)
);

CREATE TABLE IF NOT EXISTS resource_locks (
    lock_id TEXT PRIMARY KEY,
    resource_key TEXT NOT NULL UNIQUE,
    mode TEXT NOT NULL,
    holder TEXT NOT NULL,
    fencing_token INTEGER NOT NULL,
    lease_expires_at REAL NOT NULL,
    metadata TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def _json_dumps(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), default=str)


def _json_loads(raw: Optional[str], default: Any = None) -> Any:
    if raw is None or raw == "":
        return default
    return json.loads(raw)


def _normalize_str(value: Any) -> str:
    return str(value).strip().lower()


class BusStore:
    """Durable coordination store (SQLite)."""

    def __init__(self, db_path: Optional[str] = None):
        self.db_path = db_path or os.environ.get("BUS_DB_PATH", ":memory:")
        if self.db_path != ":memory:":
            parent = os.path.dirname(os.path.abspath(self.db_path))
            if parent:
                os.makedirs(parent, exist_ok=True)
        self._lock = threading.RLock()
        self._new_message = threading.Condition(self._lock)
        self._fencing_seq = 0
        # check_same_thread=False: Flask may serve requests on worker threads.
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._init_schema()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def reset(self) -> None:
        """Clear all coordination state (tests)."""
        with self._lock:
            for table in ("bus_messages", "agent_endpoints", "agents", "resource_locks", "meta"):
                self._conn.execute(f"DELETE FROM {table}")
            self._conn.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES('message_seq', '0')"
            )
            self._conn.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES('fencing_seq', '0')"
            )
            self._conn.commit()
            self._fencing_seq = 0
            self._new_message.notify_all()

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.executescript(SCHEMA_SQL)
            row = self._conn.execute(
                "SELECT value FROM meta WHERE key='message_seq'"
            ).fetchone()
            if not row:
                self._conn.execute(
                    "INSERT INTO meta(key, value) VALUES('message_seq', '0')"
                )
            row = self._conn.execute(
                "SELECT value FROM meta WHERE key='fencing_seq'"
            ).fetchone()
            if not row:
                self._conn.execute(
                    "INSERT INTO meta(key, value) VALUES('fencing_seq', '0')"
                )
            else:
                self._fencing_seq = int(row["value"])
            self._conn.commit()

    def _next_seq(self) -> int:
        row = self._conn.execute(
            "SELECT value FROM meta WHERE key='message_seq'"
        ).fetchone()
        seq = int(row["value"]) + 1
        self._conn.execute(
            "UPDATE meta SET value=? WHERE key='message_seq'", (str(seq),)
        )
        return seq

    def _next_fencing(self) -> int:
        self._fencing_seq += 1
        self._conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES('fencing_seq', ?)",
            (str(self._fencing_seq),),
        )
        return self._fencing_seq

    def _row_to_message(self, row: sqlite3.Row) -> Dict[str, Any]:
        msg: Dict[str, Any] = {
            "id": row["id"],
            "ts": row["ts"],
            "protocol": row["protocol"],
            "channel": row["channel"],
            "sender": row["sender"],
            "message": _json_loads(row["message"]),
            "type": row["type"],
            "queue": row["queue"] or "",
            "priority": row["priority"],
            "status": row["status"],
            "leased_by": row["leased_by"] or "",
            "lease_until": row["lease_until"] or 0,
            "lease_id": row["lease_id"] or "",
            "seq": row["seq"],
        }
        for key in ("conversation_id", "reply_to", "recipient", "group", "ttl_seconds"):
            val = row[key]
            if val is not None:
                msg[key] = val
        headers = _json_loads(row["headers"])
        if headers is not None:
            msg["headers"] = headers
        return msg

    # ------------------------------------------------------------------ messages

    def message_count(self) -> int:
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) AS c FROM bus_messages").fetchone()
            return int(row["c"])

    def add_message(self, payload: Dict[str, Any], status: str) -> Dict[str, Any]:
        now = time.time()
        msg_id = uuid.uuid4().hex
        message_body = payload["message"]
        if isinstance(message_body, (dict, list)):
            stored_message = _json_dumps(message_body)
        else:
            # Keep scalars as JSON so round-trip preserves type.
            stored_message = _json_dumps(message_body)

        with self._lock:
            seq = self._next_seq()
            self._conn.execute(
                """
                INSERT INTO bus_messages (
                    id, ts, protocol, channel, sender, message, type, queue,
                    priority, status, leased_by, lease_until, lease_id,
                    conversation_id, reply_to, recipient, "group", headers,
                    ttl_seconds, seq
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '', 0, '', ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    msg_id,
                    now,
                    payload.get("protocol") or "iac-bus/1.0",
                    payload["channel"],
                    payload["sender"],
                    stored_message,
                    payload.get("type", "event"),
                    payload.get("queue") or "",
                    payload.get("priority", 0),
                    status,
                    payload.get("conversation_id"),
                    payload.get("reply_to"),
                    payload.get("recipient"),
                    payload.get("group"),
                    _json_dumps(payload["headers"]) if payload.get("headers") is not None else None,
                    payload.get("ttl_seconds"),
                    seq,
                ),
            )
            self._conn.commit()
            row = self._conn.execute(
                "SELECT * FROM bus_messages WHERE id=?", (msg_id,)
            ).fetchone()
            msg = self._row_to_message(row)
            self._new_message.notify_all()
            return msg

    def prune(self, now: Optional[float] = None, retention_seconds: float = 3600, max_messages: int = 500) -> None:
        now = now if now is not None else time.time()
        with self._lock:
            # Expire leases -> pending
            self._conn.execute(
                """
                UPDATE bus_messages
                SET status='pending', leased_by='', lease_until=0, lease_id=''
                WHERE status='leased' AND lease_until <= ?
                """,
                (now,),
            )
            rows = self._conn.execute("SELECT id, ts, ttl_seconds FROM bus_messages").fetchall()
            expired_ids = []
            for row in rows:
                ttl = row["ttl_seconds"]
                if ttl is not None and now - row["ts"] > ttl:
                    expired_ids.append(row["id"])
                elif now - row["ts"] > retention_seconds:
                    expired_ids.append(row["id"])
            if expired_ids:
                self._conn.executemany(
                    "DELETE FROM bus_messages WHERE id=?",
                    [(i,) for i in expired_ids],
                )
            count = self._conn.execute("SELECT COUNT(*) AS c FROM bus_messages").fetchone()["c"]
            if count > max_messages:
                overflow = count - max_messages
                old = self._conn.execute(
                    "SELECT id FROM bus_messages ORDER BY seq ASC LIMIT ?",
                    (overflow,),
                ).fetchall()
                self._conn.executemany(
                    "DELETE FROM bus_messages WHERE id=?",
                    [(r["id"],) for r in old],
                )
            self._conn.commit()

    def list_messages(
        self,
        channel: str = "",
        since_id: str = "",
        include_queue: bool = False,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        with self._lock:
            return self._list_messages_locked(channel, since_id, include_queue, limit)

    def _list_messages_locked(
        self,
        channel: str,
        since_id: str,
        include_queue: bool,
        limit: int,
    ) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM bus_messages WHERE 1=1"
        params: List[Any] = []
        if channel:
            sql += " AND channel=?"
            params.append(channel)
        if not include_queue:
            sql += " AND (queue IS NULL OR queue='')"
        if since_id:
            row = self._conn.execute(
                "SELECT seq FROM bus_messages WHERE id=?", (since_id,)
            ).fetchone()
            if row:
                sql += " AND seq > ?"
                params.append(row["seq"])
        sql += " ORDER BY seq ASC"
        rows = self._conn.execute(sql, params).fetchall()
        msgs = [self._row_to_message(r) for r in rows]
        if limit > 0:
            msgs = msgs[-limit:]
        return msgs

    def wait_for_messages(
        self,
        channel: str = "",
        since_id: str = "",
        include_queue: bool = False,
        limit: int = 50,
        wait_seconds: float = 0,
    ) -> List[Dict[str, Any]]:
        deadline = time.time() + max(0.0, wait_seconds)
        with self._lock:
            while True:
                msgs = self._list_messages_locked(channel, since_id, include_queue, limit)
                if msgs or wait_seconds <= 0:
                    return msgs
                remaining = deadline - time.time()
                if remaining <= 0:
                    return []
                self._new_message.wait(timeout=remaining)

    def claim_queue(
        self, queue: str, worker: str, lease_seconds: float, now: Optional[float] = None
    ) -> Optional[Dict[str, Any]]:
        now = now if now is not None else time.time()
        with self._lock:
            self._conn.execute(
                """
                UPDATE bus_messages
                SET status='pending', leased_by='', lease_until=0, lease_id=''
                WHERE queue=? AND status='leased' AND lease_until <= ?
                """,
                (queue, now),
            )
            row = self._conn.execute(
                """
                SELECT * FROM bus_messages
                WHERE queue=? AND status='pending'
                ORDER BY priority DESC, seq ASC
                LIMIT 1
                """,
                (queue,),
            ).fetchone()
            if not row:
                self._conn.commit()
                return None
            lease_id = uuid.uuid4().hex
            lease_until = now + lease_seconds
            self._conn.execute(
                """
                UPDATE bus_messages
                SET status='leased', leased_by=?, lease_until=?, lease_id=?
                WHERE id=?
                """,
                (worker, lease_until, lease_id, row["id"]),
            )
            self._conn.commit()
            updated = self._conn.execute(
                "SELECT * FROM bus_messages WHERE id=?", (row["id"],)
            ).fetchone()
            return self._row_to_message(updated)

    def ack_queue(
        self,
        queue: str,
        message_id: str,
        worker: str,
        lease_id: str,
        requeue: bool,
    ) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM bus_messages WHERE id=?", (message_id,)
            ).fetchone()
            if not row or row["queue"] != queue:
                return None, "not found"
            if row["status"] != "leased":
                return None, "message not leased"
            if row["leased_by"] != worker:
                return None, "lease owner mismatch"
            if row["lease_id"] != lease_id:
                return None, "lease id mismatch"
            msg = self._row_to_message(row)
            if requeue:
                self._conn.execute(
                    """
                    UPDATE bus_messages
                    SET status='pending', leased_by='', lease_until=0, lease_id=''
                    WHERE id=?
                    """,
                    (message_id,),
                )
                self._conn.commit()
                msg = self._row_to_message(
                    self._conn.execute(
                        "SELECT * FROM bus_messages WHERE id=?", (message_id,)
                    ).fetchone()
                )
                return msg, None
            self._conn.execute("DELETE FROM bus_messages WHERE id=?", (message_id,))
            self._conn.commit()
            return msg, None

    # ------------------------------------------------------------------ agents

    def register_agent(self, data: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], Optional[Tuple[str, int, Dict[str, Any]]]]:
        """
        Returns (result, None) on success or (None, (error, status, extra)).
        """
        errors = []
        brand = data.get("brand")
        repo_locale = data.get("repo_locale")
        ordinal_path = data.get("ordinal_path")
        role = data.get("role")
        medium = data.get("medium")
        parent_agent_uuid = data.get("parent_agent_uuid")
        session_id = data.get("session_id")

        for field in ("brand", "repo_locale", "ordinal_path", "role", "medium"):
            if not data.get(field) or not isinstance(data.get(field), str):
                errors.append(f"{field} required")

        if errors:
            return None, ("invalid register payload", 400, {"details": errors})

        brand = _normalize_str(brand)
        repo_locale = _normalize_str(repo_locale)
        role = _normalize_str(role)
        medium = _normalize_str(medium)
        ordinal_path = str(ordinal_path).strip()
        if session_id is not None:
            session_id = str(session_id).strip() or None
        if parent_agent_uuid is not None:
            parent_agent_uuid = str(parent_agent_uuid).strip() or None

        if not ORDINAL_RE.match(ordinal_path):
            return None, (
                "invalid register payload",
                400,
                {"details": ["ordinal_path must match ^([0-9]+)(-[0-9]+)*$"]},
            )
        if medium not in VALID_MEDIA:
            return None, (
                "invalid register payload",
                400,
                {"details": [f"medium must be one of {sorted(VALID_MEDIA)}"]},
            )
        if ordinal_path != "0" and not parent_agent_uuid:
            return None, (
                "invalid register payload",
                400,
                {"details": ["parent_agent_uuid required when ordinal_path != '0'"]},
            )

        logical_handle = f"agent:{brand}.{repo_locale}.{ordinal_path}"
        endpoint_handle = f"{logical_handle}@{medium}"
        now = time.time()

        with self._lock:
            if parent_agent_uuid:
                parent = self._conn.execute(
                    "SELECT agent_uuid FROM agents WHERE agent_uuid=?",
                    (parent_agent_uuid,),
                ).fetchone()
                if not parent:
                    return None, ("parent agent not found", 404, {})

            existing = self._conn.execute(
                "SELECT * FROM agents WHERE logical_handle=?",
                (logical_handle,),
            ).fetchone()

            if existing:
                if (
                    existing["brand"] != brand
                    or existing["repo_locale"] != repo_locale
                    or existing["ordinal_path"] != ordinal_path
                ):
                    return None, (
                        "agent registration conflict",
                        409,
                        {
                            "conflict_code": "HANDLE_IDENTITY_MISMATCH",
                            "existing_agent_uuid": existing["agent_uuid"],
                        },
                    )
                existing_parent = existing["parent_agent_uuid"]
                if (existing_parent or None) != (parent_agent_uuid or None):
                    return None, (
                        "agent registration conflict",
                        409,
                        {
                            "conflict_code": "HANDLE_PARENT_MISMATCH",
                            "existing_agent_uuid": existing["agent_uuid"],
                        },
                    )
                if existing["role"] != role:
                    return None, (
                        "agent registration conflict",
                        409,
                        {
                            "conflict_code": "HANDLE_IDENTITY_MISMATCH",
                            "existing_agent_uuid": existing["agent_uuid"],
                        },
                    )
                agent_uuid = existing["agent_uuid"]
                created = False
                self._conn.execute(
                    "UPDATE agents SET updated_at=?, last_seen_at=?, status='active' WHERE agent_uuid=?",
                    (now, now, agent_uuid),
                )
            else:
                agent_uuid = uuid.uuid4().hex
                created = True
                self._conn.execute(
                    """
                    INSERT INTO agents (
                        agent_uuid, brand, repo_locale, ordinal_path, logical_handle,
                        parent_agent_uuid, role, status, metadata, created_at, updated_at, last_seen_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 'active', '{}', ?, ?, ?)
                    """,
                    (
                        agent_uuid,
                        brand,
                        repo_locale,
                        ordinal_path,
                        logical_handle,
                        parent_agent_uuid,
                        role,
                        now,
                        now,
                        now,
                    ),
                )

            # Upsert endpoint (session_id uniqueness: treat NULL as '')
            session_key = session_id or ""
            ep = self._conn.execute(
                """
                SELECT * FROM agent_endpoints
                WHERE agent_uuid=? AND medium=? AND IFNULL(session_id, '')=?
                """,
                (agent_uuid, medium, session_key),
            ).fetchone()
            if ep:
                self._conn.execute(
                    """
                    UPDATE agent_endpoints
                    SET last_seen_at=?, updated_at=?, is_active=1, endpoint_handle=?
                    WHERE endpoint_uuid=?
                    """,
                    (now, now, endpoint_handle if not session_id else f"{endpoint_handle}:{session_id}", ep["endpoint_uuid"]),
                )
            else:
                ep_handle = endpoint_handle if not session_id else f"{endpoint_handle}:{session_id}"
                # Avoid UNIQUE collision on endpoint_handle when session differs
                conflict = self._conn.execute(
                    "SELECT endpoint_uuid FROM agent_endpoints WHERE endpoint_handle=?",
                    (ep_handle,),
                ).fetchone()
                if conflict:
                    ep_handle = f"{ep_handle}:{uuid.uuid4().hex[:8]}"
                self._conn.execute(
                    """
                    INSERT INTO agent_endpoints (
                        endpoint_uuid, agent_uuid, medium, endpoint_handle,
                        session_id, is_active, last_seen_at, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?)
                    """,
                    (
                        uuid.uuid4().hex,
                        agent_uuid,
                        medium,
                        ep_handle,
                        session_id,
                        now,
                        now,
                        now,
                    ),
                )
                endpoint_handle = ep_handle

            self._conn.commit()
            return (
                {
                    "agent_uuid": agent_uuid,
                    "logical_handle": logical_handle,
                    "endpoint_handle": endpoint_handle,
                    "created": created,
                },
                None,
            )

    def heartbeat(self, data: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], Optional[Tuple[str, int]]]:
        agent_uuid = data.get("agent_uuid")
        medium = data.get("medium")
        session_id = data.get("session_id")
        if not isinstance(agent_uuid, str) or not agent_uuid.strip():
            return None, ("agent_uuid required", 400)
        if not isinstance(medium, str) or not medium.strip():
            return None, ("medium required", 400)
        medium = _normalize_str(medium)
        if medium not in VALID_MEDIA:
            return None, ("invalid medium", 400)
        if session_id is not None:
            session_id = str(session_id).strip() or None
        session_key = session_id or ""
        now = time.time()
        with self._lock:
            agent = self._conn.execute(
                "SELECT agent_uuid FROM agents WHERE agent_uuid=?",
                (agent_uuid.strip(),),
            ).fetchone()
            if not agent:
                return None, ("agent not found", 404)
            ep = self._conn.execute(
                """
                SELECT * FROM agent_endpoints
                WHERE agent_uuid=? AND medium=? AND IFNULL(session_id, '')=?
                """,
                (agent_uuid.strip(), medium, session_key),
            ).fetchone()
            if not ep:
                return None, ("endpoint not found", 404)
            self._conn.execute(
                "UPDATE agent_endpoints SET last_seen_at=?, updated_at=?, is_active=1 WHERE endpoint_uuid=?",
                (now, now, ep["endpoint_uuid"]),
            )
            self._conn.execute(
                "UPDATE agents SET last_seen_at=?, updated_at=? WHERE agent_uuid=?",
                (now, now, agent_uuid.strip()),
            )
            self._conn.commit()
            return {"success": True, "last_seen_at": now}, None

    def get_agent(self, agent_uuid: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM agents WHERE agent_uuid=?", (agent_uuid,)
            ).fetchone()
            if not row:
                return None
            endpoints = self._conn.execute(
                "SELECT medium, endpoint_handle, session_id, last_seen_at, is_active FROM agent_endpoints WHERE agent_uuid=?",
                (agent_uuid,),
            ).fetchall()
            return {
                "agent_uuid": row["agent_uuid"],
                "brand": row["brand"],
                "repo_locale": row["repo_locale"],
                "ordinal_path": row["ordinal_path"],
                "logical_handle": row["logical_handle"],
                "parent_agent_uuid": row["parent_agent_uuid"],
                "role": row["role"],
                "status": row["status"],
                "last_seen_at": row["last_seen_at"],
                "endpoints": [dict(e) for e in endpoints],
            }

    # ------------------------------------------------------------------ locks

    def _lock_row_to_dict(self, row: sqlite3.Row) -> Dict[str, Any]:
        return {
            "lock_id": row["lock_id"],
            "resource_key": row["resource_key"],
            "mode": row["mode"],
            "holder": row["holder"],
            "fencing_token": row["fencing_token"],
            "lease_expires_at": row["lease_expires_at"],
            "metadata": _json_loads(row["metadata"], default={}),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def acquire_lock(
        self,
        resource_key: str,
        holder: str,
        mode: str = "exclusive",
        lease_seconds: float = 60,
        metadata: Optional[Dict[str, Any]] = None,
        now: Optional[float] = None,
    ) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
        now = now if now is not None else time.time()
        mode = _normalize_str(mode)
        if mode not in VALID_LOCK_MODES:
            return None, "mode must be exclusive or shared"
        if lease_seconds <= 0:
            return None, "lease_seconds must be > 0"
        resource_key = resource_key.strip()
        holder = holder.strip()
        if not resource_key or not holder:
            return None, "resource_key and holder required"

        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM resource_locks WHERE resource_key=?",
                (resource_key,),
            ).fetchone()
            if row and row["lease_expires_at"] > now:
                if row["holder"] == holder and row["mode"] == mode:
                    # Idempotent renew-on-acquire for same holder
                    self._conn.execute(
                        """
                        UPDATE resource_locks
                        SET lease_expires_at=?, updated_at=?
                        WHERE lock_id=?
                        """,
                        (now + lease_seconds, now, row["lock_id"]),
                    )
                    self._conn.commit()
                    updated = self._conn.execute(
                        "SELECT * FROM resource_locks WHERE lock_id=?",
                        (row["lock_id"],),
                    ).fetchone()
                    return self._lock_row_to_dict(updated), None
                return None, "lock held"
            # Expired or missing — take over
            fencing = self._next_fencing()
            lock_id = uuid.uuid4().hex
            meta_raw = _json_dumps(metadata or {})
            if row:
                self._conn.execute("DELETE FROM resource_locks WHERE lock_id=?", (row["lock_id"],))
            self._conn.execute(
                """
                INSERT INTO resource_locks (
                    lock_id, resource_key, mode, holder, fencing_token,
                    lease_expires_at, metadata, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    lock_id,
                    resource_key,
                    mode,
                    holder,
                    fencing,
                    now + lease_seconds,
                    meta_raw,
                    now,
                    now,
                ),
            )
            self._conn.commit()
            updated = self._conn.execute(
                "SELECT * FROM resource_locks WHERE lock_id=?", (lock_id,)
            ).fetchone()
            return self._lock_row_to_dict(updated), None

    def renew_lock(
        self,
        resource_key: str,
        holder: str,
        fencing_token: int,
        lease_seconds: float = 60,
        now: Optional[float] = None,
    ) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
        now = now if now is not None else time.time()
        if lease_seconds <= 0:
            return None, "lease_seconds must be > 0"
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM resource_locks WHERE resource_key=?",
                (resource_key.strip(),),
            ).fetchone()
            if not row or row["lease_expires_at"] <= now:
                return None, "lock not held"
            if row["holder"] != holder.strip():
                return None, "lock holder mismatch"
            if int(row["fencing_token"]) != int(fencing_token):
                return None, "fencing token mismatch"
            self._conn.execute(
                """
                UPDATE resource_locks
                SET lease_expires_at=?, updated_at=?
                WHERE lock_id=?
                """,
                (now + lease_seconds, now, row["lock_id"]),
            )
            self._conn.commit()
            updated = self._conn.execute(
                "SELECT * FROM resource_locks WHERE lock_id=?",
                (row["lock_id"],),
            ).fetchone()
            return self._lock_row_to_dict(updated), None

    def release_lock(
        self,
        resource_key: str,
        holder: str,
        fencing_token: int,
        now: Optional[float] = None,
    ) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
        now = now if now is not None else time.time()
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM resource_locks WHERE resource_key=?",
                (resource_key.strip(),),
            ).fetchone()
            if not row:
                return None, "lock not held"
            if row["lease_expires_at"] <= now:
                self._conn.execute(
                    "DELETE FROM resource_locks WHERE lock_id=?", (row["lock_id"],)
                )
                self._conn.commit()
                return None, "lock not held"
            if row["holder"] != holder.strip():
                return None, "lock holder mismatch"
            if int(row["fencing_token"]) != int(fencing_token):
                return None, "fencing token mismatch"
            lock = self._lock_row_to_dict(row)
            self._conn.execute(
                "DELETE FROM resource_locks WHERE lock_id=?", (row["lock_id"],)
            )
            self._conn.commit()
            return lock, None

    def get_lock(self, resource_key: str, now: Optional[float] = None) -> Optional[Dict[str, Any]]:
        now = now if now is not None else time.time()
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM resource_locks WHERE resource_key=?",
                (resource_key.strip(),),
            ).fetchone()
            if not row:
                return None
            if row["lease_expires_at"] <= now:
                return None
            return self._lock_row_to_dict(row)


def create_store(db_path: Optional[str] = None) -> BusStore:
    return BusStore(db_path=db_path)
