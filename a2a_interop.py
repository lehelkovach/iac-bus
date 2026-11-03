#!/usr/bin/env python3
"""
A2A 1.0 interop helpers for iac-bus (no Flask dependency).

Design and decisions: docs/A2A_MAPPING.md. Spec: https://a2a-protocol.org
(1.0.0, wire version "1.0", a2a.proto normative). This module holds:

* constants for task states, roles and JSON-RPC error codes;
* converters between A2A wire JSON (camelCase, ISO 8601 UTC with ms,
  prefixed enums, one-of parts) and bus payloads;
* the bus's own Agent Card, and validation of cards fetched from others;
* an SSRF-guarded card fetcher (https only, no private addresses unless the
  host is allow-listed);
* ``MemoryA2ATaskTable``: the A2A task records that link an A2A task id to
  the queue item or orchestration job doing the work;
* ``A2AClient``: a minimal JSON-RPC client for calling other A2A agents
  (used by scripts/a2a_bridge.py).
"""

from __future__ import annotations

import ipaddress
import json
import socket
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import urlparse

A2A_VERSION = "1.0"
VERSION_HEADER = "A2A-Version"
CARD_PATH = "/.well-known/agent-card.json"

SUBMITTED = "TASK_STATE_SUBMITTED"
WORKING = "TASK_STATE_WORKING"
COMPLETED = "TASK_STATE_COMPLETED"
FAILED = "TASK_STATE_FAILED"
CANCELED = "TASK_STATE_CANCELED"
INPUT_REQUIRED = "TASK_STATE_INPUT_REQUIRED"
REJECTED = "TASK_STATE_REJECTED"
AUTH_REQUIRED = "TASK_STATE_AUTH_REQUIRED"
TERMINAL = frozenset({COMPLETED, FAILED, CANCELED, REJECTED})
INTERRUPTED = frozenset({INPUT_REQUIRED, AUTH_REQUIRED})
ALL_STATES = (SUBMITTED, WORKING, COMPLETED, FAILED, CANCELED, INPUT_REQUIRED, REJECTED, AUTH_REQUIRED)

ROLE_USER = "ROLE_USER"
ROLE_AGENT = "ROLE_AGENT"

SKILL_ENQUEUE = "enqueue-work"
SKILL_RUN_JOB = "run-job"
SKILLS = (SKILL_ENQUEUE, SKILL_RUN_JOB)
DEFAULT_QUEUE = "a2a-inbound"

# JSON-RPC 2.0 standard codes and A2A 1.0 codes (spec §5.4).
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
TASK_NOT_FOUND = -32001
TASK_NOT_CANCELABLE = -32002
PUSH_NOT_SUPPORTED = -32003
UNSUPPORTED_OPERATION = -32004
CONTENT_TYPE_NOT_SUPPORTED = -32005
INVALID_AGENT_RESPONSE = -32006
EXTENDED_CARD_NOT_CONFIGURED = -32007
VERSION_NOT_SUPPORTED = -32009
# Implementation-defined server error (JSON-RPC reserves -32000..-32099).
PERMISSION_DENIED = -32000

_REASONS = {
    PARSE_ERROR: "PARSE_ERROR",
    INVALID_REQUEST: "INVALID_REQUEST",
    METHOD_NOT_FOUND: "METHOD_NOT_FOUND",
    INVALID_PARAMS: "INVALID_PARAMS",
    INTERNAL_ERROR: "INTERNAL_ERROR",
    TASK_NOT_FOUND: "TASK_NOT_FOUND",
    TASK_NOT_CANCELABLE: "TASK_NOT_CANCELABLE",
    PUSH_NOT_SUPPORTED: "PUSH_NOTIFICATION_NOT_SUPPORTED",
    UNSUPPORTED_OPERATION: "UNSUPPORTED_OPERATION",
    CONTENT_TYPE_NOT_SUPPORTED: "CONTENT_TYPE_NOT_SUPPORTED",
    INVALID_AGENT_RESPONSE: "INVALID_AGENT_RESPONSE",
    EXTENDED_CARD_NOT_CONFIGURED: "EXTENDED_AGENT_CARD_NOT_CONFIGURED",
    VERSION_NOT_SUPPORTED: "VERSION_NOT_SUPPORTED",
    PERMISSION_DENIED: "PERMISSION_DENIED",
}

# Methods defined by A2A 1.0 that this bus does not implement, and the
# error each returns.
UNSUPPORTED_METHODS = {
    "SendStreamingMessage": UNSUPPORTED_OPERATION,
    "SubscribeToTask": UNSUPPORTED_OPERATION,
    "CreateTaskPushNotificationConfig": PUSH_NOT_SUPPORTED,
    "GetTaskPushNotificationConfig": PUSH_NOT_SUPPORTED,
    "ListTaskPushNotificationConfigs": PUSH_NOT_SUPPORTED,
    "DeleteTaskPushNotificationConfig": PUSH_NOT_SUPPORTED,
    "GetExtendedAgentCard": EXTENDED_CARD_NOT_CONFIGURED,
}


class JsonRpcError(Exception):
    def __init__(self, code: int, message: str, details: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}

    def to_dict(self) -> Dict[str, Any]:
        domain = "iac-bus" if self.code == PERMISSION_DENIED else "a2a-protocol.org"
        info = {"@type": "type.googleapis.com/google.rpc.ErrorInfo",
                "reason": _REASONS.get(self.code, "ERROR"), "domain": domain}
        if self.details:
            info["metadata"] = {k: str(v) for k, v in self.details.items()}
        return {"code": self.code, "message": self.message, "data": [info]}


def rpc_result(request_id: Any, result: Any) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def rpc_error(request_id: Any, error: JsonRpcError) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": error.to_dict()}


def iso(ts: Optional[float]) -> Optional[str]:
    """Unix seconds -> ISO 8601 UTC with millisecond precision (spec)."""
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + f"{int((ts % 1) * 1000):03d}Z"


def parse_iso(value: str) -> float:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


# --------------------------------------------------------------- messages


def parse_message(obj: Any) -> Dict[str, Any]:
    """Validate an A2A Message from a client. Raises JsonRpcError."""
    if not isinstance(obj, dict):
        raise JsonRpcError(INVALID_PARAMS, "message must be an object")
    message_id = obj.get("messageId")
    if not isinstance(message_id, str) or not message_id:
        raise JsonRpcError(INVALID_PARAMS, "message.messageId is required")
    role = obj.get("role")
    if role not in (ROLE_USER, ROLE_AGENT):
        raise JsonRpcError(INVALID_PARAMS, "message.role must be ROLE_USER or ROLE_AGENT")
    parts = obj.get("parts")
    if not isinstance(parts, list) or not parts:
        raise JsonRpcError(INVALID_PARAMS, "message.parts must be a non-empty array")
    for index, part in enumerate(parts):
        if not isinstance(part, dict):
            raise JsonRpcError(INVALID_PARAMS, f"message.parts[{index}] must be an object")
        kinds = [k for k in ("text", "raw", "url", "data") if k in part]
        if len(kinds) != 1:
            raise JsonRpcError(INVALID_PARAMS, f"message.parts[{index}] must hold exactly one of text, raw, url, data")
        if kinds[0] == "raw":
            raise JsonRpcError(CONTENT_TYPE_NOT_SUPPORTED, "raw (inline file) parts are not accepted by this bus; send a url part")
    for key in ("contextId", "taskId"):
        if key in obj and not isinstance(obj[key], str):
            raise JsonRpcError(INVALID_PARAMS, f"message.{key} must be a string")
    if "metadata" in obj and not isinstance(obj["metadata"], dict):
        raise JsonRpcError(INVALID_PARAMS, "message.metadata must be an object")
    return obj


def message_text(message: Dict[str, Any]) -> str:
    return "\n".join(p["text"] for p in message.get("parts", []) if isinstance(p.get("text"), str))


def message_data(message: Dict[str, Any]) -> List[Any]:
    return [p["data"] for p in message.get("parts", []) if "data" in p]


def message_urls(message: Dict[str, Any]) -> List[str]:
    return [p["url"] for p in message.get("parts", []) if isinstance(p.get("url"), str)]


def parts_for(value: Any) -> List[Dict[str, Any]]:
    """Bus value -> A2A parts."""
    if value is None:
        return []
    if isinstance(value, str):
        return [{"text": value}]
    return [{"data": value, "mediaType": "application/json"}]


def agent_message(text_or_value: Any, context_id: Optional[str] = None, task_id: Optional[str] = None) -> Dict[str, Any]:
    msg: Dict[str, Any] = {"messageId": uuid.uuid4().hex, "role": ROLE_AGENT, "parts": parts_for(text_or_value)}
    if context_id:
        msg["contextId"] = context_id
    if task_id:
        msg["taskId"] = task_id
    return msg


def decision_from_message(message: Dict[str, Any]) -> Dict[str, Any]:
    """An A2A reply to INPUT_REQUIRED -> an agent_contract DecisionResponse payload."""
    payload: Dict[str, Any] = {}
    for data in message_data(message):
        if isinstance(data, dict):
            for key in ("choice_id", "choiceId", "decision", "approved", "notes", "rationale"):
                if key in data:
                    payload["choice_id" if key == "choiceId" else key] = data[key]
    text = message_text(message).strip()
    if text:
        payload.setdefault("decision", text)
        lowered = text.lower()
        if "approved" not in payload and lowered in {"yes", "y", "approve", "approved", "ok", "go", "proceed"}:
            payload["approved"] = True
        elif "approved" not in payload and lowered in {"no", "n", "deny", "denied", "reject", "stop", "abort"}:
            payload["approved"] = False
    return payload


# ------------------------------------------------------------------ cards


def bus_card(base_url: str, version: str, name: str = "iac-bus", description: str = "",
             organization: str = "", organization_url: str = "") -> Dict[str, Any]:
    base = base_url.rstrip("/")
    card: Dict[str, Any] = {
        "name": name,
        "description": description or (
            "Coordination bus for coding agents. Hand it a task and it queues the work for supervised "
            "coding agents that lock files before editing and ask for approval before risky steps. "
            "Questions from those agents come back as TASK_STATE_INPUT_REQUIRED."),
        "supportedInterfaces": [{"url": f"{base}/a2a", "protocolBinding": "JSONRPC", "protocolVersion": A2A_VERSION}],
        "version": version or "0.0.0",
        "capabilities": {"streaming": False, "pushNotifications": False, "extendedAgentCard": False},
        "securitySchemes": {
            "runToken": {"httpAuthSecurityScheme": {
                "scheme": "Bearer",
                "description": "An iac-bus run token (iacrun_...) issued by the bus operator via "
                               "POST /agents/a2a/register. The shared bus token is not accepted here.",
            }},
        },
        "securityRequirements": [{"schemes": {"runToken": {"list": []}}}],
        "defaultInputModes": ["text/plain", "application/json"],
        "defaultOutputModes": ["text/plain", "application/json"],
        "skills": [
            {
                "id": SKILL_ENQUEUE,
                "name": "Enqueue work for coding agents",
                "description": "Puts the task on a bus work queue. A coding agent claims it, does it under the "
                               "bus's locks and approval policy, and acks it with a result, which comes back as "
                               "the task's artifact. Optional message metadata: queue, lockPaths.",
                "tags": ["coding", "queue", "supervised"],
                "examples": ["Add input validation to src/api/users.py and report what you changed."],
                "inputModes": ["text/plain", "application/json"],
                "outputModes": ["text/plain", "application/json"],
            },
            {
                "id": SKILL_RUN_JOB,
                "name": "Run as an orchestration job",
                "description": "Creates a single-step orchestration job on a bus queue; task state follows the "
                               "step (pending, running, completed, failed). Set metadata.skill to run-job.",
                "tags": ["orchestration", "job"],
                "inputModes": ["text/plain", "application/json"],
                "outputModes": ["text/plain", "application/json"],
            },
        ],
    }
    if organization:
        card["provider"] = {"organization": organization, "url": organization_url or base}
    return card


def validate_card(card: Any) -> List[str]:
    """Required-field checks for an Agent Card (a2a.proto REQUIRED fields)."""
    errors: List[str] = []
    if not isinstance(card, dict):
        return ["card must be a JSON object"]
    for key in ("name", "description", "version"):
        if not isinstance(card.get(key), str) or not card.get(key):
            errors.append(f"{key} is required")
    interfaces = card.get("supportedInterfaces")
    if not isinstance(interfaces, list) or not interfaces:
        errors.append("supportedInterfaces must be a non-empty array")
    else:
        for i, iface in enumerate(interfaces):
            if not isinstance(iface, dict) or not iface.get("url") or not iface.get("protocolBinding"):
                errors.append(f"supportedInterfaces[{i}] needs url and protocolBinding")
    if not isinstance(card.get("capabilities"), dict):
        errors.append("capabilities is required")
    for key in ("defaultInputModes", "defaultOutputModes"):
        if not isinstance(card.get(key), list) or not card.get(key):
            errors.append(f"{key} must be a non-empty array")
    skills = card.get("skills")
    if not isinstance(skills, list) or not skills:
        errors.append("skills must be a non-empty array")
    else:
        for i, skill in enumerate(skills):
            if not isinstance(skill, dict) or not all(skill.get(k) for k in ("id", "name", "description")) \
                    or not isinstance(skill.get("tags"), list):
                errors.append(f"skills[{i}] needs id, name, description and tags")
    return errors


def jsonrpc_endpoint(card: Dict[str, Any]) -> Optional[str]:
    """The card's JSON-RPC url for protocol version 1.0, if it offers one."""
    for iface in card.get("supportedInterfaces") or []:
        if iface.get("protocolBinding") == "JSONRPC" and str(iface.get("protocolVersion", A2A_VERSION)).startswith("1."):
            return iface.get("url")
    return None


def card_summary(card: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "name": card.get("name"),
        "version": card.get("version"),
        "endpoint": jsonrpc_endpoint(card),
        "skills": [s.get("id") for s in card.get("skills") or []],
        "security_schemes": sorted((card.get("securitySchemes") or {}).keys()),
    }


def _is_public_ip(address: str) -> bool:
    ip = ipaddress.ip_address(address)
    return not (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
                or ip.is_multicast or ip.is_unspecified)


def check_fetch_url(url: str, allow_hosts: Iterable[str] = ()) -> Tuple[str, str]:
    """SSRF guard for operator-supplied card URLs. Returns (scheme, host) or raises ValueError.

    https only, and the host must resolve to public addresses, unless the host
    is in ``allow_hosts`` (for development: localhost, a lab network).
    """
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    allowed = {h.strip().lower() for h in allow_hosts if h.strip()}
    if not host:
        raise ValueError("card_url has no host")
    if parsed.scheme != "https" and not (parsed.scheme == "http" and host in allowed):
        raise ValueError("card_url must be https (http only for hosts in BUS_A2A_ALLOW_HOSTS)")
    if host in allowed:
        return parsed.scheme, host
    try:
        infos = socket.getaddrinfo(host, parsed.port or 443, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise ValueError(f"cannot resolve {host}: {exc}") from exc
    for info in infos:
        if not _is_public_ip(info[4][0]):
            raise ValueError(f"{host} resolves to a non-public address; add it to BUS_A2A_ALLOW_HOSTS to allow")
    return parsed.scheme, host


def fetch_card(url: str, allow_hosts: Iterable[str] = (), session: Any = None, max_bytes: int = 262144,
               timeout: float = 10.0) -> Dict[str, Any]:
    """Fetch and validate an Agent Card. Raises ValueError with a readable reason."""
    import requests

    check_fetch_url(url, allow_hosts)
    http = session or requests.Session()
    try:
        resp = http.get(url, timeout=timeout, allow_redirects=False, stream=True,
                        headers={"Accept": "application/json", VERSION_HEADER: A2A_VERSION})
    except requests.RequestException as exc:
        raise ValueError(f"could not fetch card: {exc}") from exc
    if resp.status_code != 200:
        raise ValueError(f"card fetch returned HTTP {resp.status_code}")
    body = b""
    for chunk in resp.iter_content(8192):
        body += chunk
        if len(body) > max_bytes:
            raise ValueError("card is larger than 256 KB")
    try:
        card = json.loads(body.decode("utf-8"))
    except ValueError as exc:
        raise ValueError("card is not valid JSON") from exc
    errors = validate_card(card)
    if errors:
        raise ValueError("invalid agent card: " + "; ".join(errors))
    return card


# ------------------------------------------------------------------ tasks


def state_from_step_status(status: str, canceled: bool = False) -> str:
    """Bus orchestration step status -> A2A task state (docs/A2A_MAPPING.md §3.3)."""
    return {
        "pending": SUBMITTED,
        "running": WORKING,
        "completed": COMPLETED,
        "failed": FAILED,
        "timed_out": FAILED,
        "skipped": CANCELED if canceled else FAILED,
    }.get(status, WORKING)


def state_from_queue_status(status: Optional[str]) -> Optional[str]:
    """Bus queue item status -> A2A task state, or None when the item is gone."""
    return {"pending": SUBMITTED, "leased": WORKING}.get(status or "")


class MemoryA2ATaskTable:
    """A2A task records, in memory (like locks and runs)."""

    durable = False

    def __init__(self):
        self._mutex = threading.Lock()
        self._tasks: Dict[str, Dict[str, Any]] = {}

    def create(self, record: Dict[str, Any]) -> Dict[str, Any]:
        with self._mutex:
            self._tasks[record["id"]] = record
            return dict(record)

    def get(self, task_id: str) -> Optional[Dict[str, Any]]:
        with self._mutex:
            record = self._tasks.get(task_id)
            return dict(record) if record else None

    def update(self, task_id: str, **fields: Any) -> Optional[Dict[str, Any]]:
        with self._mutex:
            record = self._tasks.get(task_id)
            if record is None:
                return None
            if "state" in fields and fields["state"] != record.get("state"):
                fields.setdefault("status_ts", time.time())
            record.update(fields)
            return dict(record)

    def append(self, task_id: str, key: str, item: Any) -> None:
        with self._mutex:
            record = self._tasks.get(task_id)
            if record is not None:
                record.setdefault(key, []).append(item)

    def find(self, **match: Any) -> Optional[Dict[str, Any]]:
        with self._mutex:
            for record in self._tasks.values():
                if all(_dig(record, key) == value for key, value in match.items()):
                    return dict(record)
        return None

    def list(self, owner: Optional[str] = None) -> List[Dict[str, Any]]:
        with self._mutex:
            records = [dict(r) for r in self._tasks.values() if owner is None or r.get("owner") == owner]
        records.sort(key=lambda r: r.get("status_ts", 0), reverse=True)
        return records

    def count(self) -> int:
        with self._mutex:
            return len(self._tasks)


def _dig(record: Dict[str, Any], dotted: str) -> Any:
    value: Any = record
    for part in dotted.split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    return value


def task_to_wire(record: Dict[str, Any], history_length: Optional[int] = None,
                 include_artifacts: bool = True) -> Dict[str, Any]:
    status: Dict[str, Any] = {"state": record["state"], "timestamp": iso(record.get("status_ts"))}
    if record.get("status_message"):
        status["message"] = record["status_message"]
    task: Dict[str, Any] = {"id": record["id"], "contextId": record["context_id"], "status": status}
    if include_artifacts and record.get("artifacts"):
        task["artifacts"] = record["artifacts"]
    history = record.get("history") or []
    if history_length is None:
        task["history"] = history
    elif history_length > 0:
        task["history"] = history[-history_length:]
    task["metadata"] = {"skill": record.get("skill"), "queue": record.get("queue"),
                        "iacBus": {k: v for k, v in (record.get("binding") or {}).items()}}
    return task


# --------------------------------------------------------- outbound client


class A2AClient:
    """Minimal A2A 1.0 JSON-RPC client (for the outbound bridge)."""

    def __init__(self, url: str, token: str = "", session: Any = None, timeout: float = 60.0):
        import requests

        self.url = url
        self.session = session or requests.Session()
        self.timeout = timeout
        self.headers = {"Content-Type": "application/json", VERSION_HEADER: A2A_VERSION}
        if token:
            self.headers["Authorization"] = f"Bearer {token}"

    def call(self, method: str, params: Dict[str, Any]) -> Any:
        body = {"jsonrpc": "2.0", "id": uuid.uuid4().hex, "method": method, "params": params}
        resp = self.session.post(self.url, json=body, headers=self.headers, timeout=self.timeout)
        if resp.status_code != 200:
            raise JsonRpcError(INVALID_AGENT_RESPONSE, f"{method}: HTTP {resp.status_code} from remote agent")
        data = resp.json()
        if "error" in data:
            err = data["error"] or {}
            raise JsonRpcError(int(err.get("code", INTERNAL_ERROR)), str(err.get("message", "remote error")))
        if "result" not in data:
            raise JsonRpcError(INVALID_AGENT_RESPONSE, f"{method}: response has no result")
        return data["result"]

    def send_message(self, message: Dict[str, Any], return_immediately: bool = True,
                     metadata: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        params: Dict[str, Any] = {"message": message, "configuration": {"returnImmediately": return_immediately}}
        if metadata:
            params["metadata"] = metadata
        return self.call("SendMessage", params)

    def get_task(self, task_id: str, history_length: Optional[int] = None) -> Dict[str, Any]:
        params: Dict[str, Any] = {"id": task_id}
        if history_length is not None:
            params["historyLength"] = history_length
        return self.call("GetTask", params)

    def cancel_task(self, task_id: str) -> Dict[str, Any]:
        return self.call("CancelTask", {"id": task_id})
