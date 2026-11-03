#!/usr/bin/env python3
"""
MCP adapter for the IAC Bus (stdio).

A thin client over the bus HTTP API that gives coding agents (Claude Code,
Cursor, any MCP client) tools to lock repo paths, claim work, talk to each
other and ask for decisions. Each agent session runs its own copy of this
process.

Configuration (environment):

  BUS_URL           bus base URL (default http://127.0.0.1:8091)
  BUS_API_TOKEN     bearer token for the bus (empty when the bus has none)
  BUS_AGENT_ID      this agent's name, e.g. "claude.iac-bus.a"
                    (default "<user>@<host>")
  BUS_AGENT_SUFFIX  "auto" (default) appends "#<6 hex>" so two sessions that
                    share a config still get distinct identities; "0" keeps
                    BUS_AGENT_ID exactly as given
  BUS_REPO          lock scope (default: the origin remote's repo name, so
                    every clone and worktree of a repo shares one scope; then
                    the main checkout's folder name)
  BUS_REPO_ROOT     repo root used to turn absolute paths into repo-relative
                    ones (default: git top level of CLAUDE_PROJECT_DIR when
                    Claude Code sets it, else of the working directory)

An unexpanded ``${VAR}`` placeholder (what Claude Code leaves in a .mcp.json
``env`` entry when VAR is unset) counts as unset.
  BUS_HTTP_TIMEOUT  seconds per HTTP call, on top of any wait (default 15)

Run ``python mcp_server.py --check`` to print the resolved configuration and
ping the bus without starting the MCP server.

The tool layer (``BusTools``) has no MCP dependency, so it can later be
mounted on the bus itself as a Streamable HTTP endpoint
(docs/IDENTITY-AND-AUTHORITY.md rung ID6) without rewriting the tools.
"""

from __future__ import annotations

import functools
import getpass
import json
import os
import re
import secrets
import socket
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

import requests

from agent_contract import (
    AgentState,
    ChoiceOption,
    DecisionRequest,
    ProgressUpdate,
    request_decision_message,
    status_update_message,
)
from locks import normalize_resource, resource_covers

DEFAULT_BUS_URL = "http://127.0.0.1:8091"
DEFAULT_HTTP_TIMEOUT = 15.0
DEFAULT_LOCK_TTL = 600
MAX_WAIT_SECONDS = 30
DECISION_CHANNEL = "decisions"
STATUS_CHANNEL = "status"

_FALSEY = {"0", "false", "no", "off", "none"}


class BusError(Exception):
    """The bus could not be reached or rejected the request."""


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def _git(cwd: Path, *args: str) -> Optional[str]:
    try:
        out = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    value = out.stdout.strip()
    return value if out.returncode == 0 and value else None


def _git_toplevel(cwd: Path) -> Optional[Path]:
    top = _git(cwd, "rev-parse", "--show-toplevel")
    return Path(top) if top else None


def repo_name_from_remote(url: str) -> Optional[str]:
    """'git@github.com:owner/iac-bus.git' or 'https://.../iac-bus' -> 'iac-bus'."""
    tail = re.split(r"[/:]", url.strip().rstrip("/"))[-1]
    tail = tail[:-4] if tail.endswith(".git") else tail
    return tail or None


def detect_repo_name(cwd: Path) -> Optional[str]:
    """Lock scope shared by every clone and worktree of the same repo.

    Prefers the origin remote's repo name, then the main checkout's folder
    (so git worktrees agree with it), then the top-level folder.
    """
    remote = _git(cwd, "remote", "get-url", "origin")
    if remote and repo_name_from_remote(remote):
        return repo_name_from_remote(remote)
    common = _git(cwd, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if common and Path(common).name == ".git":
        return Path(common).parent.name
    top = _git_toplevel(cwd)
    return top.name if top else None


def _default_agent_id() -> str:
    try:
        user = getpass.getuser()
    except Exception:  # noqa: BLE001 - getuser can raise several types
        user = "agent"
    return f"{user}@{socket.gethostname()}"


def session_agent_id(base: str, suffix_mode: str = "auto") -> str:
    """The identity this process uses on the bus.

    By default a random suffix is appended, so two agent sessions that share
    one MCP config (for example a committed .mcp.json) never share an
    identity. Locks held by the same identity do not conflict, so a shared
    identity would silently disable mutual exclusion.
    """
    base = base.strip() or _default_agent_id()
    if suffix_mode.strip().lower() in _FALSEY:
        return base
    return f"{base}#{secrets.token_hex(3)}"


_PLACEHOLDER_RE = re.compile(r"^\$\{[^}]*\}$")


def _env_value(env: Mapping[str, str], key: str) -> str:
    """Read ``key``, treating an unexpanded ``${VAR}`` placeholder as unset.

    Claude Code leaves ``${VAR}`` literally in a .mcp.json ``env`` entry when
    VAR is unset and no default is given.
    """
    value = (env.get(key) or "").strip()
    return "" if _PLACEHOLDER_RE.match(value) else value


@dataclass
class Config:
    bus_url: str
    api_token: str
    agent_id: str
    repo: str
    repo_root: Path
    http_timeout: float = DEFAULT_HTTP_TIMEOUT

    @classmethod
    def from_env(cls, env: Optional[Mapping[str, str]] = None, cwd: Optional[Path] = None) -> "Config":
        env = os.environ if env is None else env
        get = functools.partial(_env_value, env)
        cwd = Path(cwd or os.getcwd()).resolve()
        # Claude Code sets CLAUDE_PROJECT_DIR for the servers it spawns; the
        # working directory it uses is not documented.
        start = Path(get("CLAUDE_PROJECT_DIR") or cwd).resolve()
        repo_root = Path(get("BUS_REPO_ROOT") or _git_toplevel(start) or start).resolve()
        repo = get("BUS_REPO") or detect_repo_name(repo_root) or repo_root.name or "default"
        return cls(
            bus_url=(get("BUS_URL") or DEFAULT_BUS_URL).rstrip("/"),
            api_token=get("BUS_API_TOKEN"),
            agent_id=session_agent_id(get("BUS_AGENT_ID"), get("BUS_AGENT_SUFFIX") or "auto"),
            repo=repo,
            repo_root=repo_root,
            http_timeout=float(get("BUS_HTTP_TIMEOUT") or DEFAULT_HTTP_TIMEOUT),
        )


# ---------------------------------------------------------------------------
# HTTP client
# ---------------------------------------------------------------------------


class BusClient:
    """Minimal HTTP client for the bus. ``session`` is injectable for tests."""

    def __init__(self, config: Config, session: Optional[Any] = None):
        self.config = config
        self.session = session or requests.Session()

    def request(
        self,
        method: str,
        path: str,
        body: Optional[Dict[str, Any]] = None,
        params: Optional[Dict[str, Any]] = None,
        wait_seconds: float = 0,
    ) -> Tuple[int, Any]:
        headers = {"Content-Type": "application/json"}
        if self.config.api_token:
            headers["Authorization"] = f"Bearer {self.config.api_token}"
        url = f"{self.config.bus_url}{path}"
        try:
            resp = self.session.request(
                method,
                url,
                json=body,
                params=params,
                headers=headers,
                timeout=self.config.http_timeout + max(wait_seconds, 0),
            )
        except requests.RequestException as exc:
            raise BusError(f"cannot reach the bus at {self.config.bus_url}: {exc}") from exc
        if resp.status_code == 401:
            raise BusError("the bus rejected the API token (401); check BUS_API_TOKEN")
        if resp.status_code >= 500:
            raise BusError(f"bus error {resp.status_code} on {method} {path}: {resp.text[:300]}")
        if resp.status_code == 204 or not resp.content:
            return resp.status_code, None
        try:
            data = resp.json()
        except ValueError:
            data = {"raw": resp.text}
        if resp.status_code == 400:
            details = data.get("details") if isinstance(data, dict) else None
            raise BusError(f"bus rejected the request: {details or data}")
        return resp.status_code, data


# ---------------------------------------------------------------------------
# Tools (no MCP dependency)
# ---------------------------------------------------------------------------


def _clamp_wait(wait_seconds: float) -> float:
    return float(max(0, min(wait_seconds or 0, MAX_WAIT_SECONDS)))


def _in_seconds(ts: Optional[float]) -> Optional[int]:
    if ts is None:
        return None
    return max(0, int(round(ts - time.time())))


def _compact_message(msg: Dict[str, Any]) -> Dict[str, Any]:
    keep = ("id", "ts", "sender", "type", "message", "recipient", "conversation_id", "reply_to")
    return {k: msg[k] for k in keep if msg.get(k) not in (None, "")}


def _slug(label: str, index: int) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")
    return slug[:40] or f"option-{index + 1}"


class BusTools:
    """The tools an agent session gets. Each public method is one MCP tool.

    The instance remembers the locks and work leases this session holds, so
    agents can release by path and ack by message id without carrying
    lock ids or lease ids around.
    """

    def __init__(self, client: BusClient, config: Config):
        self.client = client
        self.config = config
        self.agent_id = config.agent_id
        self.repo = config.repo
        self._mutex = threading.Lock()
        self._held: Dict[str, Dict[str, Any]] = {}
        self._claims: Dict[str, Dict[str, Any]] = {}
        self._cursors: Dict[str, str] = {}

    # ----------------------------------------------------------- helpers

    def to_resource(self, path: str) -> str:
        """Repo-relative, normalized form of ``path``.

        Absolute paths inside the repo root are converted; anything outside
        the repo is rejected.
        """
        if not isinstance(path, str) or not path.strip():
            raise ValueError("path required")
        raw = path.strip()
        if os.path.isabs(raw):
            root = self.config.repo_root
            try:
                raw = str(Path(raw).resolve().relative_to(root)) or "."
            except ValueError as exc:
                raise ValueError(f"{path} is outside the repo root {root}") from exc
        return normalize_resource(raw)

    def _held_matching(self, resource: str) -> List[Dict[str, Any]]:
        with self._mutex:
            return [lock for lock in self._held.values() if lock["resource"] == resource]

    def _forget(self, lock_id: str) -> None:
        with self._mutex:
            self._held.pop(lock_id, None)

    def held_locks(self) -> List[Dict[str, Any]]:
        with self._mutex:
            return [dict(lock) for lock in self._held.values()]

    def _post_message(self, body: Dict[str, Any]) -> Dict[str, Any]:
        body = {k: v for k, v in body.items() if v not in (None, "")}
        status, data = self.client.request("POST", "/bus/messages", body)
        if status != 201:
            raise BusError(f"post failed ({status}): {data}")
        return data["message"]

    # ------------------------------------------------------------- locks

    def acquire_lock(
        self,
        path: str,
        mode: str = "exclusive",
        reason: str = "",
        ttl_seconds: int = DEFAULT_LOCK_TTL,
        wait_seconds: int = 0,
    ) -> Dict[str, Any]:
        resource = self.to_resource(path)
        wait = _clamp_wait(wait_seconds)
        status, data = self.client.request(
            "POST",
            "/bus/locks/acquire",
            {
                "repo": self.repo,
                "resource": resource,
                "agent": self.agent_id,
                "mode": mode,
                "ttl_seconds": ttl_seconds,
                "reason": reason,
                "wait_seconds": wait,
            },
            wait_seconds=wait,
        )
        if status == 200:
            held = {
                "lock_id": data["lock_id"],
                "resource": data["resource"],
                "mode": data["mode"],
                "fencing_token": data["fencing_token"],
                "expires_at": data["expires_at"],
            }
            with self._mutex:
                self._held[data["lock_id"]] = held
            return {
                "acquired": True,
                "path": data["resource"],
                "repo": data["repo"],
                "mode": data["mode"],
                "fencing_token": data["fencing_token"],
                "expires_in_seconds": _in_seconds(data["expires_at"]),
                "waited_seconds": data.get("waited_seconds", 0),
                "lock_id": data["lock_id"],
                "you": self.agent_id,
                "next": "Edit the files, call check_lock before committing, then release_lock.",
            }
        if status == 409:
            return {
                "acquired": False,
                "path": resource,
                "held_by": data.get("held_by"),
                "holder_mode": data.get("mode"),
                "holder_reason": data.get("reason"),
                "conflicting_path": data.get("conflicting_resource"),
                "expires_in_seconds": _in_seconds(data.get("expires_at")),
                "waited_seconds": data.get("waited_seconds", 0),
                "conflicts": len(data.get("conflicts", [])),
                "next": (
                    "Do NOT edit files under this path. Retry with wait_seconds, work on "
                    "something else, or post_message to the holder."
                ),
            }
        raise BusError(f"unexpected acquire response {status}: {data}")

    def release_lock(self, path: str = "", lock_id: str = "", all: bool = False) -> Dict[str, Any]:  # noqa: A002
        if all:
            targets = self.held_locks()
        elif lock_id:
            targets = [{"lock_id": lock_id, "resource": path or "?"}]
        elif path:
            targets = self._held_matching(self.to_resource(path))
            if not targets:
                raise ValueError(f"this session holds no lock on {path!r}; see list_locks(mine_only=true)")
        else:
            raise ValueError("pass path, lock_id, or all=true")
        released, gone = [], []
        for lock in targets:
            status, data = self.client.request(
                "POST", "/bus/locks/release", {"lock_id": lock["lock_id"], "agent": self.agent_id}
            )
            if status == 200:
                released.append(data["lock"]["resource"])
                self._forget(lock["lock_id"])
            elif status == 404:
                gone.append(lock["resource"])
                self._forget(lock["lock_id"])
            else:
                raise BusError(f"release failed ({status}): {data}")
        result: Dict[str, Any] = {"released": released}
        if gone:
            result["already_expired"] = gone
        return result

    def renew_lock(self, path: str = "", lock_id: str = "", ttl_seconds: int = DEFAULT_LOCK_TTL) -> Dict[str, Any]:
        if lock_id:
            ids = [lock_id]
        elif path:
            ids = [lock["lock_id"] for lock in self._held_matching(self.to_resource(path))]
            if not ids:
                raise ValueError(f"this session holds no lock on {path!r}")
        else:
            raise ValueError("pass path or lock_id")
        renewed = []
        for lid in ids:
            status, data = self.client.request(
                "POST", "/bus/locks/renew", {"lock_id": lid, "agent": self.agent_id, "ttl_seconds": ttl_seconds}
            )
            if status == 200:
                with self._mutex:
                    if lid in self._held:
                        self._held[lid]["expires_at"] = data["expires_at"]
                renewed.append({"path": data["resource"], "expires_in_seconds": _in_seconds(data["expires_at"])})
            elif status == 404:
                self._forget(lid)
                return {
                    "renewed": renewed,
                    "error": "lock expired or was released; acquire_lock again and use the new fencing token",
                }
            else:
                raise BusError(f"renew failed ({status}): {data}")
        return {"renewed": renewed}

    def check_lock(self, path: str, fencing_token: Optional[int] = None) -> Dict[str, Any]:
        resource = self.to_resource(path)
        token = fencing_token
        if token is None:
            with self._mutex:
                covering = [
                    lock for lock in self._held.values() if resource_covers(lock["resource"], resource)
                ]
            if not covering:
                return {"valid": False, "path": resource,
                        "reason": "this session holds no lock covering this path; acquire_lock first"}
            token = max(lock["fencing_token"] for lock in covering)
        status, data = self.client.request(
            "POST",
            "/bus/locks/validate",
            {"repo": self.repo, "resource": resource, "fencing_token": token, "agent": self.agent_id},
        )
        if status not in (200, 409):
            raise BusError(f"validate failed ({status}): {data}")
        result = {"valid": bool(data.get("valid")), "path": resource, "fencing_token": token,
                  "reason": data.get("reason")}
        if not result["valid"]:
            result["next"] = ("Stop. Re-acquire the lock, then re-read the files: another agent may have "
                              "changed them while your lock was gone.")
        elif data.get("lock", {}).get("mode") != "exclusive":
            result["warning"] = "your lock is shared; take an exclusive lock before writing"
        return result

    def list_locks(self, path_prefix: str = "", mine_only: bool = False, all_repos: bool = False) -> Dict[str, Any]:
        params: Dict[str, Any] = {}
        if not all_repos:
            params["repo"] = self.repo
        if path_prefix:
            params["resource_prefix"] = self.to_resource(path_prefix)
        if mine_only:
            params["agent"] = self.agent_id
        _status, data = self.client.request("GET", "/bus/locks", params=params)
        locks = [
            {
                "path": lock["resource"],
                "repo": lock["repo"],
                "mode": lock["mode"],
                "held_by": lock["agent"],
                "reason": lock.get("reason", ""),
                "expires_in_seconds": _in_seconds(lock.get("expires_at")),
                "mine": lock["agent"] == self.agent_id,
            }
            for lock in data.get("locks", [])
        ]
        return {"locks": locks, "count": len(locks), "you": self.agent_id}

    # -------------------------------------------------------------- work

    def claim_work(self, queue: str, lease_seconds: int = 300) -> Dict[str, Any]:
        status, data = self.client.request(
            "POST",
            "/bus/queues/claim",
            {"queue": queue, "worker": self.agent_id, "lease_seconds": lease_seconds},
        )
        if status == 204:
            return {"claimed": False, "queue": queue}
        if status != 200:
            raise BusError(f"claim failed ({status}): {data}")
        msg = data["message"]
        with self._mutex:
            self._claims[msg["id"]] = {"queue": queue, "lease_id": msg["lease_id"]}
        return {
            "claimed": True,
            "queue": queue,
            "message_id": msg["id"],
            "lease_id": msg["lease_id"],
            "lease_expires_in_seconds": _in_seconds(msg.get("lease_until")),
            "type": msg.get("type"),
            "sender": msg.get("sender"),
            "work": msg.get("message"),
        }

    def _finish_work(self, path: str, message_id: str, lease_id: str, queue: str, extra: Dict[str, Any]) -> Dict[str, Any]:
        with self._mutex:
            tracked = self._claims.get(message_id, {})
        lease_id = lease_id or tracked.get("lease_id", "")
        queue = queue or tracked.get("queue", "")
        if not lease_id or not queue:
            raise ValueError("unknown message_id for this session; pass queue and lease_id")
        body = {"queue": queue, "message_id": message_id, "worker": self.agent_id, "lease_id": lease_id}
        body.update(extra)
        status, data = self.client.request("POST", path, body)
        if status == 200:
            with self._mutex:
                self._claims.pop(message_id, None)
            return {"ok": True, "message_id": message_id, "queue": queue}
        if status == 404:
            return {"ok": False, "message_id": message_id, "error": "message not found (lease expired and it was re-claimed or acked)"}
        return {"ok": False, "message_id": message_id, "error": (data or {}).get("error", f"status {status}")}

    def ack_work(self, message_id: str, lease_id: str = "", queue: str = "") -> Dict[str, Any]:
        return self._finish_work("/bus/queues/ack", message_id, lease_id, queue, {})

    def nack_work(self, message_id: str, requeue: bool = True, lease_id: str = "", queue: str = "") -> Dict[str, Any]:
        return self._finish_work("/bus/queues/nack", message_id, lease_id, queue, {"requeue": requeue})

    # ---------------------------------------------------------- messages

    def post_message(
        self,
        channel: str,
        message: str,
        type: str = "info",  # noqa: A002 - matches the bus field name
        recipient: str = "",
        conversation_id: str = "",
        reply_to: str = "",
        data: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        body_message: Any = {"text": message, "data": data} if data else message
        msg = self._post_message({
            "channel": channel,
            "sender": self.agent_id,
            "message": body_message,
            "type": type,
            "recipient": recipient,
            "conversation_id": conversation_id,
            "reply_to": reply_to,
        })
        return {"posted": True, "id": msg["id"], "channel": channel, "sender": msg["sender"]}

    def read_messages(
        self, channel: str, since_id: str = "", limit: int = 50, wait_seconds: int = 0
    ) -> Dict[str, Any]:
        with self._mutex:
            cursor = since_id or self._cursors.get(channel, "")
        wait = _clamp_wait(wait_seconds)
        params: Dict[str, Any] = {"channel": channel, "limit": limit}
        if cursor:
            params["since_id"] = cursor
        if wait:
            params["wait_seconds"] = wait
        _status, data = self.client.request("GET", "/bus/messages", params=params, wait_seconds=wait)
        msgs = data.get("messages", [])
        if msgs:
            with self._mutex:
                self._cursors[channel] = msgs[-1]["id"]
        return {
            "channel": channel,
            "messages": [_compact_message(m) for m in msgs],
            "count": len(msgs),
            "next_since_id": msgs[-1]["id"] if msgs else cursor,
        }

    # ------------------------------------------------- decisions / status

    def request_decision(
        self,
        question: str,
        options: Optional[List[str]] = None,
        context: str = "",
        recipient: str = "",
        channel: str = DECISION_CHANNEL,
        wait_seconds: int = 0,
    ) -> Dict[str, Any]:
        conversation_id = uuid.uuid4().hex
        choices = [ChoiceOption(option_id=_slug(label, i), label=label) for i, label in enumerate(options or [])]
        request = DecisionRequest(
            prompt=question,
            choices=choices,
            context={"text": context} if context else None,
            metadata={"repo": self.repo},
        )
        envelope = request_decision_message(
            source=self.agent_id,
            target=recipient or None,
            request=request,
            conversation_id=conversation_id,
            state=AgentState.WAITING_INPUT,
        ).to_dict()
        posted = self._post_message({
            "protocol": envelope["protocol"],
            "channel": channel,
            "sender": self.agent_id,
            "type": "request_decision",
            "message": envelope,
            "recipient": recipient,
            "conversation_id": conversation_id,
        })
        result = {
            "conversation_id": conversation_id,
            "channel": channel,
            "request_message_id": posted["id"],
            "options": [c.to_dict() for c in choices],
        }
        result.update(self.check_decision(conversation_id, channel=channel, wait_seconds=wait_seconds))
        return result

    def check_decision(self, conversation_id: str, channel: str = DECISION_CHANNEL, wait_seconds: int = 0) -> Dict[str, Any]:
        wait = _clamp_wait(wait_seconds)
        deadline = time.monotonic() + wait
        since = ""
        while True:
            params: Dict[str, Any] = {"channel": channel, "limit": 200}
            if since:
                params["since_id"] = since
            remaining = max(0.0, deadline - time.monotonic())
            if since and remaining > 0:
                params["wait_seconds"] = round(remaining, 3)
            _status, data = self.client.request("GET", "/bus/messages", params=params, wait_seconds=remaining)
            msgs = data.get("messages", [])
            for msg in msgs:
                answer = _decision_answer(msg, conversation_id)
                if answer is not None:
                    answer.update({"decided": True, "decided_by": msg.get("sender")})
                    return answer
            if msgs:
                since = msgs[-1]["id"]
            if time.monotonic() >= deadline:
                return {"decided": False,
                        "next": "Do not take the risky step yet. Call check_decision later with this conversation_id."}
            if not since:
                # Empty channel: nothing to anchor a long-poll on, so wait in slices.
                time.sleep(min(0.5, max(0.0, deadline - time.monotonic())))

    def report_status(
        self,
        state: str,
        summary: str,
        progress_pct: Optional[float] = None,
        task: str = "",
        channel: str = STATUS_CHANNEL,
    ) -> Dict[str, Any]:
        valid = [s.value for s in AgentState]
        if state not in valid:
            raise ValueError(f"state must be one of {valid}")
        update = ProgressUpdate(
            summary=summary,
            progress_pct=progress_pct,
            state=AgentState(state),
            details={"task": task} if task else None,
            metadata={"repo": self.repo, "locks_held": [lock["resource"] for lock in self.held_locks()]},
        )
        envelope = status_update_message(source=self.agent_id, target=None, update=update, state=AgentState(state)).to_dict()
        posted = self._post_message({
            "protocol": envelope["protocol"],
            "channel": channel,
            "sender": self.agent_id,
            "type": "report_status",
            "message": envelope,
        })
        return {"posted": True, "id": posted["id"], "channel": channel, "state": state}

    # ---------------------------------------------------------- lifecycle

    def release_all_quietly(self) -> None:
        """Best-effort release of every lock this session holds (process exit)."""
        for lock in self.held_locks():
            try:
                self.client.request("POST", "/bus/locks/release", {"lock_id": lock["lock_id"], "agent": self.agent_id})
            except Exception:  # noqa: BLE001 - shutting down; nothing useful to do
                pass
            self._forget(lock["lock_id"])


def _decision_answer(msg: Dict[str, Any], conversation_id: str) -> Optional[Dict[str, Any]]:
    """Return the decision payload if ``msg`` answers ``conversation_id``."""
    body = msg.get("message")
    envelope = body if isinstance(body, dict) else {}
    conv = msg.get("conversation_id") or envelope.get("conversation_id")
    if conv != conversation_id:
        return None
    action = envelope.get("action") or ""
    if msg.get("type") not in ("provide_decision", "decision") and action != "provide_decision":
        return None
    payload = envelope.get("payload") if isinstance(envelope.get("payload"), dict) else envelope
    answer = {k: payload.get(k) for k in ("choice_id", "decision", "approved", "notes", "rationale") if payload.get(k) is not None}
    if not answer and isinstance(body, str):
        answer = {"decision": body}
    return answer


# ---------------------------------------------------------------------------
# MCP server
# ---------------------------------------------------------------------------

LOCK_RULE = (
    "Other coding agents are editing this repo at the same time. ALWAYS call acquire_lock on a path "
    "BEFORE you edit, create, rename or delete any file under it, and call release_lock as soon as "
    "you have finished (after committing)."
)

TOOL_DESCRIPTIONS: Dict[str, str] = {
    "acquire_lock": (
        "REQUIRED BEFORE EDITING. Lock a repo path before you edit, create, rename or delete any file "
        "under it, so other agents working in this repo do not overwrite your work or you theirs. Lock "
        "the narrowest path that covers the change: a file ('src/app.py') or a folder ('src/auth/') for "
        "a multi-file change. Paths are repo-relative; absolute paths inside the repo are converted. "
        "If acquired is false another agent holds an overlapping lock: do NOT edit those files; retry "
        "with wait_seconds (max 30), work on something else, or post_message to the holder. On success "
        "keep the fencing_token, call check_lock before you commit, and call release_lock when you are "
        "done. Locks expire after ttl_seconds unless you call renew_lock. Use mode='shared' only for "
        "reading something you need to stay unchanged."
    ),
    "release_lock": (
        "Release a lock as soon as you have finished editing (and committed) the files under it, so other "
        "agents can proceed. Pass the same path you locked, or all=true to release every lock this "
        "session holds; do that before you finish your task."
    ),
    "renew_lock": (
        "Extend a lock you hold when your edit will outlast its ttl. Renew before it expires: an expired "
        "lock cannot be renewed, so you would have to acquire_lock again and re-read the files."
    ),
    "check_lock": (
        "Confirm right before you commit or write that your lock on this path is still live (fencing "
        "check). If valid is false your lock expired or was taken over: stop, acquire_lock again, and "
        "re-read the files because another agent may have changed them."
    ),
    "list_locks": (
        "See which agents hold locks in this repo, optionally only locks overlapping path_prefix. Use it "
        "to plan around other agents' work. A path that looks free is not reserved: you still must call "
        "acquire_lock before editing it."
    ),
    "claim_work": (
        "Take the next work item from a bus queue under a lease. Call ack_work when it is done or "
        "nack_work to hand it back. Acquire locks on the files the work touches before editing them."
    ),
    "ack_work": "Mark a claimed work item as done. Release the locks you took for it as well.",
    "nack_work": (
        "Hand a claimed work item back to the queue (requeue=true, the default) or drop it "
        "(requeue=false), for example when you are blocked or it is not yours to do."
    ),
    "post_message": (
        "Post a message to a bus channel for other agents or humans: progress, a blocker, a handoff, or "
        "a note to the agent holding a lock you need (set recipient to its id)."
    ),
    "read_messages": (
        "Read messages on a bus channel. Without since_id it continues after the last message this "
        "session read on that channel. wait_seconds (max 30) long-polls for new messages."
    ),
    "request_decision": (
        "Ask a supervisor or human to decide before a risky or irreversible step: deleting files, "
        "changing a public API or schema, merging, deploying, or anything outside your task. Give the "
        "question and options. With wait_seconds (max 30) it waits for the answer; otherwise call "
        "check_decision later with the returned conversation_id. Do not take the step without an answer."
    ),
    "check_decision": "Check whether a decision you requested with request_decision has been answered.",
    "report_status": (
        "Report your state (running, waiting_input, paused, completed, failed, ...) and a one-line "
        "summary to the status channel so supervisors and other agents know what you are doing. Call it "
        "when you start a task, when you are blocked, and when you finish."
    ),
}


def _as_tool_error(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Turn BusError/ValueError into MCP tool errors the model can read."""
    from mcp.server.mcpserver.exceptions import ToolError

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except (BusError, ValueError) as exc:
            raise ToolError(str(exc)) from exc

    return wrapper


def build_server(tools: BusTools):
    from mcp.server import MCPServer

    from version import get_version

    instructions = (
        f"{LOCK_RULE} Your identity on the bus is {tools.agent_id} in repo {tools.repo}. Workflow: "
        "report_status(running) -> acquire_lock(path) -> edit -> check_lock(path) -> commit -> "
        "release_lock(path). Ask with request_decision before risky or irreversible steps. When you "
        "finish: release_lock(all=true) and report_status(completed)."
    )
    server = MCPServer(name="iac-bus", instructions=instructions, version=get_version())
    for name, description in TOOL_DESCRIPTIONS.items():
        server.add_tool(_as_tool_error(getattr(tools, name)), name=name, description=description)
    return server


def _check(config: Config) -> int:
    print(json.dumps({
        "bus_url": config.bus_url,
        "agent_id": config.agent_id,
        "repo": config.repo,
        "repo_root": str(config.repo_root),
        "api_token_set": bool(config.api_token),
    }, indent=2))
    client = BusClient(config)
    try:
        status, health = client.request("GET", "/health")
        print(f"health: {status} {health.get('status') if isinstance(health, dict) else health}")
        status, locks = client.request("GET", "/bus/locks", params={"repo": config.repo})
        print(f"locks in {config.repo}: {locks.get('count')}")
    except BusError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    config = Config.from_env()
    if "--check" in argv:
        return _check(config)
    tools = BusTools(BusClient(config), config)
    server = build_server(tools)
    print(
        f"iac-bus MCP adapter: agent={config.agent_id} repo={config.repo} bus={config.bus_url}",
        file=sys.stderr,
    )
    try:
        server.run()
    finally:
        tools.release_all_quietly()
    return 0


if __name__ == "__main__":
    sys.exit(main())
