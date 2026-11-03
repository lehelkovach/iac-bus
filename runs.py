#!/usr/bin/env python3
"""
Runs and run tokens (identity-plan rungs ID1-ID2, minimal).

A *run* is one execution of an agent: a Claude Code session, a `claude -p`
call, a Cursor agent. Whoever holds the shared ``BUS_API_TOKEN`` (a human, a
launcher, or the MCP adapter at startup) registers a run and gets back an
opaque **run token**. On every later request that presents the run token, the
bus derives the caller's identity (the run's ``actor``) from the token and
ignores the ``sender`` / ``worker`` / ``agent`` names in the request body. One
run cannot act as another, and a leaked run token is limited to one run for
its lifetime.

Rules:

* ``actor`` is ``<handle>#<first 6 hex of run_id>``: readable and unique per run.
* Tokens look like ``iacrun_<random>``. Only their SHA-256 hash is stored.
* A run expires ``ttl_seconds`` after it was last used (sliding expiry); any
  authenticated request or a heartbeat extends it. Ending a run revokes its
  token immediately.
* The table is in memory in both store backends, like locks. After a bus
  restart, run tokens are unknown and clients register again.

Not here yet (Track B in docs/IDENTITY-AND-AUTHORITY.md): principals, grants,
child runs minted by runs, signatures.
"""

from __future__ import annotations

import hashlib
import secrets
import threading
import time
import uuid
from typing import Any, Callable, Dict, List, Optional

TOKEN_PREFIX = "iacrun_"

STATUS_ACTIVE = "active"
STATUS_ENDED = "ended"
STATUS_EXPIRED = "expired"


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def looks_like_run_token(token: str) -> bool:
    return isinstance(token, str) and token.startswith(TOKEN_PREFIX)


def public_run(run: Dict[str, Any]) -> Dict[str, Any]:
    """A run record without its token hash."""
    return {k: v for k, v in run.items() if k != "token_hash"}


class MemoryRunTable:
    durable = False

    def __init__(self, clock: Callable[[], float] = time.time):
        self._clock = clock
        self._mutex = threading.Lock()
        self._runs: Dict[str, Dict[str, Any]] = {}
        self._by_hash: Dict[str, str] = {}

    def _expire_locked(self, now: float) -> None:
        for run in self._runs.values():
            if run["status"] == STATUS_ACTIVE and run["expires_at"] <= now:
                run["status"] = STATUS_EXPIRED
                run["ended_at"] = run["expires_at"]

    def create(
        self,
        handle: str,
        agent_uuid: str,
        ttl_seconds: float,
        role: str = "worker",
        medium: str = "",
        session_id: str = "",
        grants: Optional[Dict[str, List[str]]] = None,
    ) -> tuple[Dict[str, Any], str]:
        """Create a run. Returns ``(public record, run_token)``; the token is shown once.

        ``grants`` restricts the run (deny by default): lists of fnmatch
        patterns under ``skills``, ``queues``, ``channels`` and repo-relative
        ``lock_paths``. ``None`` means an unrestricted run, as before grants
        existed. Registered A2A agents always get grants.
        """
        now = self._clock()
        run_id = uuid.uuid4().hex
        token = TOKEN_PREFIX + secrets.token_urlsafe(32)
        run = {
            "run_id": run_id,
            "agent_uuid": agent_uuid,
            "handle": handle,
            "actor": f"{handle}#{run_id[:6]}",
            "role": role,
            "medium": medium,
            "session_id": session_id,
            "status": STATUS_ACTIVE,
            "started_at": now,
            "last_seen_at": now,
            "expires_at": now + ttl_seconds,
            "ttl_seconds": ttl_seconds,
            "ended_at": None,
            "grants": grants,
            "token_hash": hash_token(token),
        }
        with self._mutex:
            self._runs[run_id] = run
            self._by_hash[run["token_hash"]] = run_id
        return public_run(run), token

    def authenticate(self, token: str) -> tuple[Optional[Dict[str, Any]], Optional[str]]:
        """Resolve a run token. Slides the expiry on success.

        Returns ``(run, None)`` or ``(None, reason)``.
        """
        now = self._clock()
        with self._mutex:
            self._expire_locked(now)
            run_id = self._by_hash.get(hash_token(token))
            run = self._runs.get(run_id) if run_id else None
            if run is None:
                return None, "unknown run token"
            if run["status"] != STATUS_ACTIVE:
                return None, f"run {run['status']}"
            run["last_seen_at"] = now
            run["expires_at"] = now + run["ttl_seconds"]
            return public_run(run), None

    def heartbeat(self, run_id: str) -> Optional[Dict[str, Any]]:
        now = self._clock()
        with self._mutex:
            self._expire_locked(now)
            run = self._runs.get(run_id)
            if run is None or run["status"] != STATUS_ACTIVE:
                return None
            run["last_seen_at"] = now
            run["expires_at"] = now + run["ttl_seconds"]
            return public_run(run)

    def end(self, run_id: str) -> Optional[Dict[str, Any]]:
        now = self._clock()
        with self._mutex:
            self._expire_locked(now)
            run = self._runs.get(run_id)
            if run is None or run["status"] != STATUS_ACTIVE:
                return None
            run["status"] = STATUS_ENDED
            run["ended_at"] = now
            return public_run(run)

    def get(self, run_id: str) -> Optional[Dict[str, Any]]:
        with self._mutex:
            self._expire_locked(self._clock())
            run = self._runs.get(run_id)
            return public_run(run) if run else None

    def list(self, active_only: bool = True) -> List[Dict[str, Any]]:
        with self._mutex:
            self._expire_locked(self._clock())
            runs = [public_run(r) for r in self._runs.values() if not active_only or r["status"] == STATUS_ACTIVE]
        runs.sort(key=lambda r: r["started_at"])
        return runs

    def active_count(self) -> int:
        return len(self.list(active_only=True))
