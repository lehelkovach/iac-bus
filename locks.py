#!/usr/bin/env python3
"""
Path locks with fencing tokens (ladder rung L4, identity-plan rung ID3).

Coding agents working on one repository take a lock on a path before they
edit it, and release it when they are done. The rules:

* A resource is a repo-relative path. ``src/auth`` and ``src/auth/`` are the
  same resource, and a lock on a folder covers everything below it.
* Two resources overlap when one is the other or one is a parent folder of the
  other, compared on whole path segments. ``src/auth`` overlaps
  ``src/auth/login.py`` but not ``src/authz.py``. ``.`` is the repo root and
  overlaps everything in that repo.
* Locks are scoped by ``repo``. The same path in two repos never conflicts.
* ``exclusive`` conflicts with every overlapping lock. ``shared`` conflicts
  only with an overlapping ``exclusive`` lock, so many agents can hold shared
  locks on the same path at once.
* Locks held by the same agent never conflict with each other, so an agent can
  take a folder lock and then a file lock inside it.
* Every successful acquire gets a new fencing token, strictly greater than any
  token issued before it in this process. Tokens start from the current time
  in milliseconds, so they keep increasing across a restart too.
* A lock expires at ``expires_at`` unless its holder renews it. Only the
  holder (same ``agent`` and the ``lock_id`` returned by acquire) can renew or
  release it.
* A fencing token is valid only while its lock is live. ``validate`` is the
  write-boundary check: after a lock expires and another agent takes the path,
  the old holder's token fails validation.

Lock ids are capabilities: they are returned to the acquirer only and are left
out of every listing, event and conflict report.

The table is in memory in both store backends (see ``store.py``).

TODO(L5): ``docs/sql/ACP_V2_SCHEMA.sql`` declares
``resource_locks.resource_key`` as ``UNIQUE``. That allows one row per
resource, so it cannot represent several holders of a shared lock on the same
path. Before locks move to SQL the table needs a ``(resource_key, holder)``
key or a separate holders table.
"""

from __future__ import annotations

import posixpath
import re
import threading
import time
import uuid
from typing import Any, Callable, Dict, List, Optional, Tuple

MODE_EXCLUSIVE = "exclusive"
MODE_SHARED = "shared"
MODES = (MODE_EXCLUSIVE, MODE_SHARED)

DEFAULT_REPO = "default"
ROOT_RESOURCE = "."

EVENT_ACQUIRED = "acquired"
EVENT_RELEASED = "released"
EVENT_EXPIRED = "expired"

ERR_NOT_FOUND = "lock not found or expired"
ERR_OWNER = "lock owner mismatch"

_REPO_RE = re.compile(r"^[A-Za-z0-9._-]+(/[A-Za-z0-9._-]+)?$")
_GLOB_CHARS = set("*?[]")
_WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:")
_WAIT_SLICE_SECONDS = 0.25


def normalize_resource(raw: Any) -> str:
    """Return the canonical form of a repo-relative path, or raise ValueError."""
    if not isinstance(raw, str):
        raise ValueError("resource must be string")
    value = raw.strip().replace("\\", "/")
    if not value:
        raise ValueError("resource required")
    if value.startswith("/") or _WINDOWS_DRIVE_RE.match(value):
        raise ValueError("resource must be a repo-relative path, not an absolute path")
    if _GLOB_CHARS & set(value):
        raise ValueError("resource must be a literal path; lock the folder instead of a glob")
    norm = posixpath.normpath(value)
    if norm == ".." or norm.startswith("../"):
        raise ValueError("resource must stay inside the repo")
    return norm


def normalize_repo(raw: Any) -> str:
    """Return the repo scope for a lock. Missing or empty means ``default``."""
    if raw is None:
        return DEFAULT_REPO
    if not isinstance(raw, str):
        raise ValueError("repo must be string")
    value = raw.strip()
    if not value:
        return DEFAULT_REPO
    if not _REPO_RE.match(value):
        raise ValueError("repo must look like 'name' or 'owner/name'")
    return value


def resource_parts(resource: str) -> Tuple[str, ...]:
    if resource == ROOT_RESOURCE:
        return ()
    return tuple(resource.split("/"))


def resources_overlap(a: str, b: str) -> bool:
    """True when one path is the other or a parent folder of the other."""
    pa, pb = resource_parts(a), resource_parts(b)
    n = min(len(pa), len(pb))
    return pa[:n] == pb[:n]


def resource_covers(lock_resource: str, target: str) -> bool:
    """True when a lock on ``lock_resource`` covers ``target``."""
    pl, pt = resource_parts(lock_resource), resource_parts(target)
    return pt[: len(pl)] == pl


def public_view(lock: Dict[str, Any]) -> Dict[str, Any]:
    """A lock record without its lock id, safe to show to anyone."""
    return {k: v for k, v in lock.items() if k != "lock_id"}


class MemoryLockTable:
    """Thread-safe in-memory lock table.

    ``clock`` supplies wall-clock seconds for lease expiry and is injectable
    for tests. Waiting in ``acquire`` uses ``time.monotonic`` so a fake clock
    cannot stall a waiter.

    ``on_event(kind, lock_public_view)`` is called for acquired, released and
    expired locks, after the table's own lock is released, so it may publish
    to the bus without deadlocking.
    """

    durable = False

    def __init__(
        self,
        clock: Callable[[], float] = time.time,
        first_token: Optional[int] = None,
        on_event: Optional[Callable[[str, Dict[str, Any]], None]] = None,
    ):
        self._clock = clock
        self._cond = threading.Condition(threading.Lock())
        self._locks: Dict[str, Dict[str, Any]] = {}
        self._next_token = int(first_token if first_token is not None else time.time() * 1000)
        self.on_event = on_event

    # ----------------------------------------------------------- internals

    def _emit(self, events: List[Tuple[str, Dict[str, Any]]]) -> None:
        if not self.on_event:
            return
        for kind, lock in events:
            self.on_event(kind, lock)

    def _expire_locked(self, now: float) -> List[Tuple[str, Dict[str, Any]]]:
        expired = [lock for lock in self._locks.values() if lock["expires_at"] <= now]
        for lock in expired:
            del self._locks[lock["lock_id"]]
        if expired:
            self._cond.notify_all()
        return [(EVENT_EXPIRED, public_view(lock)) for lock in expired]

    def _conflicts_locked(self, repo: str, resource: str, mode: str, agent: str) -> List[Dict[str, Any]]:
        conflicts = []
        for lock in self._locks.values():
            if lock["repo"] != repo or lock["agent"] == agent:
                continue
            if not resources_overlap(lock["resource"], resource):
                continue
            if mode == MODE_EXCLUSIVE or lock["mode"] == MODE_EXCLUSIVE:
                conflicts.append(lock)
        conflicts.sort(key=lambda lock: lock["fencing_token"])
        return [public_view(lock) for lock in conflicts]

    # ------------------------------------------------------------- public

    def sweep(self) -> int:
        """Expire stale locks now. Returns how many expired."""
        with self._cond:
            events = self._expire_locked(self._clock())
        self._emit(events)
        return len(events)

    def acquire(
        self,
        repo: str,
        resource: str,
        agent: str,
        mode: str,
        ttl_seconds: float,
        reason: str = "",
        wait_seconds: float = 0.0,
    ) -> Tuple[Optional[Dict[str, Any]], List[Dict[str, Any]], float]:
        """Try to take a lock, waiting up to ``wait_seconds`` for conflicts to clear.

        Returns ``(lock, conflicts, waited_seconds)``. ``lock`` is the full
        record including ``lock_id`` on success, else None with the
        conflicting locks' public views.
        """
        if mode not in MODES:
            raise ValueError(f"mode must be one of {MODES}")
        started = time.monotonic()
        deadline = started + max(wait_seconds, 0.0)
        events: List[Tuple[str, Dict[str, Any]]] = []
        lock: Optional[Dict[str, Any]] = None
        with self._cond:
            while True:
                now = self._clock()
                events.extend(self._expire_locked(now))
                conflicts = self._conflicts_locked(repo, resource, mode, agent)
                if not conflicts:
                    lock = {
                        "lock_id": uuid.uuid4().hex,
                        "repo": repo,
                        "resource": resource,
                        "mode": mode,
                        "agent": agent,
                        "reason": reason,
                        "fencing_token": self._next_token,
                        "acquired_at": now,
                        "renewed_at": now,
                        "expires_at": now + ttl_seconds,
                        "ttl_seconds": ttl_seconds,
                    }
                    self._next_token += 1
                    self._locks[lock["lock_id"]] = lock
                    events.append((EVENT_ACQUIRED, public_view(lock)))
                    lock = dict(lock)
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._cond.wait(timeout=min(remaining, _WAIT_SLICE_SECONDS))
        self._emit(events)
        waited = round(time.monotonic() - started, 3)
        return lock, ([] if lock else conflicts), waited

    def renew(
        self, lock_id: str, agent: str, ttl_seconds: float
    ) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
        with self._cond:
            now = self._clock()
            events = self._expire_locked(now)
            lock = self._locks.get(lock_id)
            if lock is None:
                result, error = None, ERR_NOT_FOUND
            elif lock["agent"] != agent:
                result, error = None, ERR_OWNER
            else:
                lock["renewed_at"] = now
                lock["expires_at"] = now + ttl_seconds
                lock["ttl_seconds"] = ttl_seconds
                result, error = dict(lock), None
        self._emit(events)
        return result, error

    def release(self, lock_id: str, agent: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
        with self._cond:
            now = self._clock()
            events = self._expire_locked(now)
            lock = self._locks.get(lock_id)
            if lock is None:
                result, error = None, ERR_NOT_FOUND
            elif lock["agent"] != agent:
                result, error = None, ERR_OWNER
            else:
                del self._locks[lock_id]
                self._cond.notify_all()
                view = public_view(lock)
                view["released_at"] = now
                events.append((EVENT_RELEASED, view))
                result, error = dict(lock), None
        self._emit(events)
        return result, error

    def validate(
        self,
        repo: str,
        resource: str,
        fencing_token: int,
        agent: Optional[str] = None,
    ) -> Tuple[bool, str, Optional[Dict[str, Any]]]:
        """Write-boundary check: is ``fencing_token`` a live lock covering ``resource``?"""
        with self._cond:
            events = self._expire_locked(self._clock())
            match = None
            for lock in self._locks.values():
                if lock["fencing_token"] == fencing_token:
                    match = lock
                    break
            if match is None:
                result = (False, "no live lock has this fencing token (released, expired or never issued)", None)
            elif match["repo"] != repo:
                result = (False, "fencing token belongs to a lock in another repo", public_view(match))
            elif not resource_covers(match["resource"], resource):
                result = (False, "the lock for this fencing token does not cover this resource", public_view(match))
            elif agent is not None and match["agent"] != agent:
                result = (False, "the lock for this fencing token is held by another agent", public_view(match))
            else:
                result = (True, "ok", public_view(match))
        self._emit(events)
        return result

    def list(
        self,
        repo: Optional[str] = None,
        resource_prefix: Optional[str] = None,
        agent: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Public views of live locks, oldest token first.

        ``resource_prefix`` returns every lock that overlaps that path,
        including locks on its parent folders.
        """
        with self._cond:
            events = self._expire_locked(self._clock())
            locks = []
            for lock in self._locks.values():
                if repo is not None and lock["repo"] != repo:
                    continue
                if agent is not None and lock["agent"] != agent:
                    continue
                if resource_prefix is not None and not resources_overlap(lock["resource"], resource_prefix):
                    continue
                locks.append(public_view(lock))
        self._emit(events)
        locks.sort(key=lambda lock: lock["fencing_token"])
        return locks

    def count(self) -> int:
        with self._cond:
            now = self._clock()
            return sum(1 for lock in self._locks.values() if lock["expires_at"] > now)
