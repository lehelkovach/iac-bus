#!/usr/bin/env python3
"""
Storage backends for the IAC Bus.

Two interchangeable backends sit behind the same small interface:

* ``MemoryStore`` (default) keeps messages in a plain list and agent records
  in a dict. It is exactly the behaviour the bus has always had: a restart
  clears everything.
* ``SqliteStore`` (opt-in, ``IAC_BUS_STORE=sqlite``) persists the same records
  to a SQLite file named by ``IAC_BUS_DB`` so queued messages, leases and
  agent registrations survive a process restart. Ported from the closed
  PR #8 branch (``cursor/iac-bus-v0-1-coordination-ba28``) and trimmed to
  what the current HTTP API needs; the lock/endpoint tables from that branch
  were left behind.

Each backend also carries ``locks`` (``locks.py``), ``runs`` (``runs.py``) and
``a2a_tasks`` (``a2a_interop.py``). They work in memory; with SQLite they also
write through ``SqliteJournal`` and reload on startup, so locks (with the
fencing counter), run tokens and A2A tasks survive a restart. ``/health``
reports ``locks_durable``.

Both backends keep the message dict shape the HTTP API already returns, so
switching backend never changes a response body. Orchestration job state and
the metrics counters stay in memory in either mode.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

from locks import MemoryLockTable
from a2a_interop import MemoryA2ATaskTable
from runs import MemoryRunTable

STATUS_PUBLISHED = "published"
STATUS_PENDING = "pending"
STATUS_LEASED = "leased"

BACKEND_MEMORY = "memory"
BACKEND_SQLITE = "sqlite"
BACKENDS = (BACKEND_MEMORY, BACKEND_SQLITE)

DEFAULT_DB_PATH = "iac-bus.db"


def _message_is_expired(message: Dict[str, Any], now: float, retention_seconds: float) -> bool:
    ttl_seconds = message.get("ttl_seconds")
    if ttl_seconds is not None and now - message["ts"] > ttl_seconds:
        return True
    return now - message["ts"] > retention_seconds


def _release(message: Dict[str, Any]) -> None:
    message["status"] = STATUS_PENDING
    message["leased_by"] = ""
    message["lease_until"] = 0
    message["lease_id"] = ""


class MemoryStore:
    """In-memory backend. Operates on the list/dict handed in so callers
    (and tests) that hold a reference to them keep seeing live state."""

    backend = BACKEND_MEMORY
    durable = False

    def __init__(
        self,
        messages: Optional[List[Dict[str, Any]]] = None,
        agents: Optional[Dict[str, Dict[str, Any]]] = None,
    ):
        self.messages: List[Dict[str, Any]] = messages if messages is not None else []
        self.agents: Dict[str, Dict[str, Any]] = agents if agents is not None else {}
        self._lock = threading.RLock()
        # Path locks (locks.py) and runs (runs.py). In memory in both backends.
        self.locks = MemoryLockTable()
        self.runs = MemoryRunTable()
        self.a2a_tasks = MemoryA2ATaskTable()

    def close(self) -> None:
        return None

    # ---------------------------------------------------------------- messages

    def add_message(self, message: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock:
            self.messages.append(message)
        return message

    def prune(self, now: float, retention_seconds: float, max_messages: int) -> None:
        with self._lock:
            for message in self.messages:
                if message.get("status") == STATUS_LEASED and message.get("lease_until", 0) <= now:
                    _release(message)
            self.messages[:] = [
                m for m in self.messages if not _message_is_expired(m, now, retention_seconds)
            ]
            if len(self.messages) > max_messages:
                self.messages[:] = self.messages[-max_messages:]

    def list_messages(
        self, channel: str, since_id: str, include_queue: bool, limit: int
    ) -> List[Dict[str, Any]]:
        with self._lock:
            msgs = list(self.messages)
        if channel:
            msgs = [m for m in msgs if m["channel"] == channel]
        if not include_queue:
            msgs = [m for m in msgs if not m.get("queue")]
        if since_id:
            try:
                idx = next(i for i, m in enumerate(msgs) if m["id"] == since_id)
                msgs = msgs[idx + 1:]
            except StopIteration:
                pass
        return msgs[-limit:]

    def claim_queue(
        self, queue: str, worker: str, lease_seconds: float, now: float
    ) -> Optional[Dict[str, Any]]:
        with self._lock:
            for message in self.messages:
                if message.get("queue") != queue:
                    continue
                if message.get("status") == STATUS_LEASED:
                    if message.get("lease_until", 0) > now:
                        continue
                    _release(message)
                if message.get("status") != STATUS_PENDING:
                    continue
                message["status"] = STATUS_LEASED
                message["leased_by"] = worker
                message["lease_until"] = now + lease_seconds
                message["lease_id"] = uuid.uuid4().hex
                return message
        return None

    def ack_queue(
        self, queue: str, message_id: str, worker: str, lease_id: str, requeue: bool
    ) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
        with self._lock:
            for idx, message in enumerate(self.messages):
                if message.get("id") != message_id:
                    continue
                if message.get("queue") != queue:
                    continue
                if message.get("status") != STATUS_LEASED:
                    return None, "message not leased"
                if message.get("leased_by") != worker:
                    return None, "lease owner mismatch"
                if message.get("lease_id") != lease_id:
                    return None, "lease id mismatch"
                if requeue:
                    _release(message)
                    return message, None
                self.messages.pop(idx)
                return message, None
        return None, "not found"

    def get_message(self, message_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            for message in self.messages:
                if message.get("id") == message_id:
                    return dict(message)
        return None

    def delete_message(self, message_id: str, only_pending: bool = True) -> bool:
        """Remove a message (used to withdraw a queued item nobody has claimed)."""
        with self._lock:
            for idx, message in enumerate(self.messages):
                if message.get("id") != message_id:
                    continue
                if only_pending and message.get("status") != STATUS_PENDING:
                    return False
                self.messages.pop(idx)
                return True
        return False

    def renew_lease(
        self, queue: str, message_id: str, worker: str, lease_id: str, lease_seconds: float, now: float
    ) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
        """Extend a live lease held by ``worker``; an expired lease cannot be renewed."""
        with self._lock:
            for message in self.messages:
                if message.get("id") != message_id or message.get("queue") != queue:
                    continue
                if message.get("status") != STATUS_LEASED or message.get("lease_until", 0) <= now:
                    return None, "lease expired"
                if message.get("leased_by") != worker:
                    return None, "lease owner mismatch"
                if message.get("lease_id") != lease_id:
                    return None, "lease id mismatch"
                message["lease_until"] = now + lease_seconds
                return dict(message), None
        return None, "not found"

    def message_count(self) -> int:
        with self._lock:
            return len(self.messages)

    def queue_counts(self, now: float) -> Tuple[int, int]:
        pending = 0
        leased = 0
        with self._lock:
            for message in self.messages:
                if not message.get("queue"):
                    continue
                status = message.get("status")
                lease_until = message.get("lease_until", 0)
                if status == STATUS_LEASED and lease_until > now:
                    leased += 1
                elif status == STATUS_PENDING or (status == STATUS_LEASED and lease_until <= now):
                    pending += 1
        return pending, leased

    # ------------------------------------------------------------------ agents

    def put_agent(self, record: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock:
            self.agents[record["agent_uuid"]] = record
        return record

    def get_agent(self, agent_uuid: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            return self.agents.get(agent_uuid)

    def list_agents(self) -> List[Dict[str, Any]]:
        with self._lock:
            return sorted(self.agents.values(), key=lambda r: r.get("registered_at", 0))

    def agent_count(self) -> int:
        with self._lock:
            return len(self.agents)


_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS messages (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    id TEXT NOT NULL UNIQUE,
    ts REAL NOT NULL,
    channel TEXT NOT NULL,
    queue TEXT NOT NULL DEFAULT '',
    ttl_seconds REAL,
    status TEXT NOT NULL,
    leased_by TEXT NOT NULL DEFAULT '',
    lease_until REAL NOT NULL DEFAULT 0,
    lease_id TEXT NOT NULL DEFAULT '',
    body TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_channel_seq ON messages(channel, seq);
CREATE INDEX IF NOT EXISTS idx_messages_queue_status ON messages(queue, status);

CREATE TABLE IF NOT EXISTS agents (
    agent_uuid TEXT PRIMARY KEY,
    agent_handle TEXT NOT NULL,
    registered_at REAL NOT NULL,
    record TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_agents_handle ON agents(agent_handle);

CREATE TABLE IF NOT EXISTS durable_records (
    kind TEXT NOT NULL,
    key TEXT NOT NULL,
    record TEXT NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY (kind, key)
);
"""

# How long finished records are kept in the journal.
ENDED_RUN_RETENTION_SECONDS = 7 * 86400
FINISHED_TASK_RETENTION_SECONDS = 30 * 86400
_TERMINAL_A2A = ("TASK_STATE_COMPLETED", "TASK_STATE_FAILED", "TASK_STATE_CANCELED", "TASK_STATE_REJECTED")


class SqliteJournal:
    """Write-through persistence for the in-memory lock, run and A2A task tables.

    Records are JSON keyed by (kind, key). The tables keep working in memory
    and call save/delete on every change; at startup they load() their kind.
    Shares the store's connection and lock.
    """

    def __init__(self, conn: sqlite3.Connection, lock: "threading.RLock"):
        self._conn = conn
        self._lock = lock

    def save(self, kind: str, key: str, record: Dict[str, Any]) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO durable_records (kind, key, record, updated_at) VALUES (?, ?, ?, ?)",
                (kind, key, json.dumps(record, separators=(",", ":")), time.time()),
            )
            self._conn.commit()

    def delete(self, kind: str, key: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM durable_records WHERE kind=? AND key=?", (kind, key))
            self._conn.commit()

    def get(self, kind: str, key: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT record FROM durable_records WHERE kind=? AND key=?", (kind, key)
            ).fetchone()
        return json.loads(row["record"]) if row else None

    def load(self, kind: str) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT record FROM durable_records WHERE kind=? ORDER BY updated_at ASC", (kind,)
            ).fetchall()
        return [json.loads(r["record"]) for r in rows]

    def prune(self, now: float) -> int:
        """Drop runs that ended long ago and A2A tasks that finished long ago."""
        removed = 0
        for kind, cutoff, done in (
            ("run", now - ENDED_RUN_RETENTION_SECONDS, lambda r: r.get("status") != "active"),
            ("a2a_task", now - FINISHED_TASK_RETENTION_SECONDS, lambda r: r.get("state") in _TERMINAL_A2A),
        ):
            with self._lock:
                rows = self._conn.execute(
                    "SELECT key, record FROM durable_records WHERE kind=? AND updated_at < ?", (kind, cutoff)
                ).fetchall()
                stale = [r["key"] for r in rows if done(json.loads(r["record"]))]
                for key in stale:
                    self._conn.execute("DELETE FROM durable_records WHERE kind=? AND key=?", (kind, key))
                self._conn.commit()
            removed += len(stale)
        return removed


class SqliteStore:
    """SQLite backend. Same interface and the same message dict shape as
    ``MemoryStore``; rows round-trip through JSON so a message read back is
    equal to the one that was written."""

    backend = BACKEND_SQLITE
    durable = True

    def __init__(self, db_path: str = DEFAULT_DB_PATH):
        self.db_path = db_path
        if db_path != ":memory:":
            parent = os.path.dirname(os.path.abspath(db_path))
            if parent:
                os.makedirs(parent, exist_ok=True)
        self._lock = threading.RLock()
        # Locks, runs and A2A tasks work in memory and write through this
        # journal, so they survive a restart (memory stays authoritative: the
        # bus is a single process).
        # check_same_thread=False: Flask serves requests on worker threads;
        # every access is serialised by self._lock.
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(_SCHEMA_SQL)
            self._conn.commit()
        self.journal = SqliteJournal(self._conn, self._lock)
        self.journal.prune(time.time())
        self.locks = MemoryLockTable(journal=self.journal)
        self.runs = MemoryRunTable(journal=self.journal)
        self.a2a_tasks = MemoryA2ATaskTable(journal=self.journal)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @staticmethod
    def _row_to_message(row: sqlite3.Row) -> Dict[str, Any]:
        msg = json.loads(row["body"])
        msg["status"] = row["status"]
        msg["leased_by"] = row["leased_by"] or ""
        msg["lease_until"] = row["lease_until"] or 0
        msg["lease_id"] = row["lease_id"] or ""
        return msg

    def _fetch(self, message_id: str) -> Optional[sqlite3.Row]:
        return self._conn.execute("SELECT * FROM messages WHERE id=?", (message_id,)).fetchone()

    # ---------------------------------------------------------------- messages

    def add_message(self, message: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO messages (
                    id, ts, channel, queue, ttl_seconds, status,
                    leased_by, lease_until, lease_id, body
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    message["id"],
                    message["ts"],
                    message["channel"],
                    message.get("queue") or "",
                    message.get("ttl_seconds"),
                    message["status"],
                    message.get("leased_by", ""),
                    message.get("lease_until", 0),
                    message.get("lease_id", ""),
                    json.dumps(message, separators=(",", ":")),
                ),
            )
            self._conn.commit()
        return message

    def prune(self, now: float, retention_seconds: float, max_messages: int) -> None:
        with self._lock:
            self._conn.execute(
                """
                UPDATE messages
                SET status=?, leased_by='', lease_until=0, lease_id=''
                WHERE status=? AND lease_until <= ?
                """,
                (STATUS_PENDING, STATUS_LEASED, now),
            )
            self._conn.execute(
                """
                DELETE FROM messages
                WHERE (ttl_seconds IS NOT NULL AND ? - ts > ttl_seconds)
                   OR ? - ts > ?
                """,
                (now, now, retention_seconds),
            )
            self._conn.execute(
                """
                DELETE FROM messages
                WHERE seq NOT IN (SELECT seq FROM messages ORDER BY seq DESC LIMIT ?)
                """,
                (max_messages,),
            )
            self._conn.commit()

    def list_messages(
        self, channel: str, since_id: str, include_queue: bool, limit: int
    ) -> List[Dict[str, Any]]:
        where = ["1=1"]
        params: List[Any] = []
        if channel:
            where.append("channel=?")
            params.append(channel)
        if not include_queue:
            where.append("queue=''")
        clause = " AND ".join(where)
        with self._lock:
            if since_id:
                # Same rule as the in-memory list: the anchor only applies when
                # it is itself part of the filtered view.
                row = self._conn.execute(
                    f"SELECT seq FROM messages WHERE {clause} AND id=?",
                    params + [since_id],
                ).fetchone()
                if row:
                    clause += " AND seq > ?"
                    params.append(row["seq"])
            rows = self._conn.execute(
                f"SELECT * FROM messages WHERE {clause} ORDER BY seq DESC LIMIT ?",
                params + [max(limit, 0)],
            ).fetchall()
        return [self._row_to_message(r) for r in reversed(rows)]

    def claim_queue(
        self, queue: str, worker: str, lease_seconds: float, now: float
    ) -> Optional[Dict[str, Any]]:
        with self._lock:
            self._conn.execute(
                """
                UPDATE messages
                SET status=?, leased_by='', lease_until=0, lease_id=''
                WHERE queue=? AND status=? AND lease_until <= ?
                """,
                (STATUS_PENDING, queue, STATUS_LEASED, now),
            )
            row = self._conn.execute(
                "SELECT id FROM messages WHERE queue=? AND status=? ORDER BY seq ASC LIMIT 1",
                (queue, STATUS_PENDING),
            ).fetchone()
            if not row:
                self._conn.commit()
                return None
            lease_id = uuid.uuid4().hex
            self._conn.execute(
                "UPDATE messages SET status=?, leased_by=?, lease_until=?, lease_id=? WHERE id=?",
                (STATUS_LEASED, worker, now + lease_seconds, lease_id, row["id"]),
            )
            self._conn.commit()
            return self._row_to_message(self._fetch(row["id"]))

    def ack_queue(
        self, queue: str, message_id: str, worker: str, lease_id: str, requeue: bool
    ) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
        with self._lock:
            row = self._fetch(message_id)
            if not row or row["queue"] != queue:
                return None, "not found"
            if row["status"] != STATUS_LEASED:
                return None, "message not leased"
            if row["leased_by"] != worker:
                return None, "lease owner mismatch"
            if row["lease_id"] != lease_id:
                return None, "lease id mismatch"
            if requeue:
                self._conn.execute(
                    "UPDATE messages SET status=?, leased_by='', lease_until=0, lease_id='' WHERE id=?",
                    (STATUS_PENDING, message_id),
                )
                self._conn.commit()
                return self._row_to_message(self._fetch(message_id)), None
            message = self._row_to_message(row)
            self._conn.execute("DELETE FROM messages WHERE id=?", (message_id,))
            self._conn.commit()
            return message, None

    def get_message(self, message_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._fetch(message_id)
        return self._row_to_message(row) if row else None

    def delete_message(self, message_id: str, only_pending: bool = True) -> bool:
        with self._lock:
            if only_pending:
                cur = self._conn.execute("DELETE FROM messages WHERE id=? AND status=?", (message_id, STATUS_PENDING))
            else:
                cur = self._conn.execute("DELETE FROM messages WHERE id=?", (message_id,))
            self._conn.commit()
            return cur.rowcount > 0

    def renew_lease(
        self, queue: str, message_id: str, worker: str, lease_id: str, lease_seconds: float, now: float
    ) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
        with self._lock:
            row = self._fetch(message_id)
            if not row or row["queue"] != queue:
                return None, "not found"
            if row["status"] != STATUS_LEASED or (row["lease_until"] or 0) <= now:
                return None, "lease expired"
            if row["leased_by"] != worker:
                return None, "lease owner mismatch"
            if row["lease_id"] != lease_id:
                return None, "lease id mismatch"
            self._conn.execute("UPDATE messages SET lease_until=? WHERE id=?", (now + lease_seconds, message_id))
            self._conn.commit()
            return self._row_to_message(self._fetch(message_id)), None

    def message_count(self) -> int:
        with self._lock:
            return int(self._conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0])

    def queue_counts(self, now: float) -> Tuple[int, int]:
        with self._lock:
            leased = self._conn.execute(
                "SELECT COUNT(*) FROM messages WHERE queue<>'' AND status=? AND lease_until > ?",
                (STATUS_LEASED, now),
            ).fetchone()[0]
            pending = self._conn.execute(
                """
                SELECT COUNT(*) FROM messages
                WHERE queue<>'' AND (status=? OR (status=? AND lease_until <= ?))
                """,
                (STATUS_PENDING, STATUS_LEASED, now),
            ).fetchone()[0]
        return int(pending), int(leased)

    # ------------------------------------------------------------------ agents

    def put_agent(self, record: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO agents (agent_uuid, agent_handle, registered_at, record) VALUES (?, ?, ?, ?)",
                (
                    record["agent_uuid"],
                    record.get("agent_handle", ""),
                    record.get("registered_at", time.time()),
                    json.dumps(record, separators=(",", ":")),
                ),
            )
            self._conn.commit()
        return record

    def get_agent(self, agent_uuid: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT record FROM agents WHERE agent_uuid=?", (agent_uuid,)
            ).fetchone()
        return json.loads(row["record"]) if row else None

    def list_agents(self) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT record FROM agents ORDER BY registered_at ASC, agent_uuid ASC"
            ).fetchall()
        return [json.loads(r["record"]) for r in rows]

    def agent_count(self) -> int:
        with self._lock:
            return int(self._conn.execute("SELECT COUNT(*) FROM agents").fetchone()[0])


def create_store(
    backend: Optional[str] = None,
    db_path: Optional[str] = None,
    messages: Optional[List[Dict[str, Any]]] = None,
    agents: Optional[Dict[str, Dict[str, Any]]] = None,
):
    """Build the store selected by ``IAC_BUS_STORE`` / ``IAC_BUS_DB``.

    Explicit arguments win over the environment. Unknown backend names fail
    loudly at startup rather than silently falling back to memory.
    """
    backend = (backend or os.environ.get("IAC_BUS_STORE") or BACKEND_MEMORY).strip().lower()
    if backend == BACKEND_MEMORY:
        return MemoryStore(messages=messages, agents=agents)
    if backend == BACKEND_SQLITE:
        return SqliteStore(db_path or os.environ.get("IAC_BUS_DB") or DEFAULT_DB_PATH)
    raise ValueError(f"IAC_BUS_STORE must be one of {BACKENDS}, got {backend!r}")
