#!/usr/bin/env python3
"""
Inter-Agent Communication Bus (IAC Bus).
"""

import fnmatch
import hmac
import json
import logging
import os
import re
import resource
import threading
import time
import uuid
from collections import deque

from flask import Flask, g, jsonify, request

from locks import (
    EVENT_EXPIRED,
    ERR_NOT_FOUND,
    MODE_EXCLUSIVE,
    MODES,
    normalize_repo,
    normalize_resource,
    public_view,
    resource_covers,
)
from a2a_interop import (
    A2A_VERSION,
    CARD_PATH,
    COMPLETED,
    DEFAULT_QUEUE,
    FAILED,
    CANCELED,
    INPUT_REQUIRED,
    INTERRUPTED,
    INVALID_PARAMS,
    INVALID_REQUEST,
    METHOD_NOT_FOUND,
    PARSE_ERROR,
    PERMISSION_DENIED,
    PUSH_NOT_SUPPORTED,
    ROLE_USER,
    SKILL_ENQUEUE,
    SKILLS,
    SUBMITTED,
    TASK_NOT_CANCELABLE,
    TASK_NOT_FOUND,
    TERMINAL,
    UNSUPPORTED_METHODS,
    UNSUPPORTED_OPERATION,
    VERSION_HEADER,
    VERSION_NOT_SUPPORTED,
    WORKING,
    ALL_STATES,
    JsonRpcError,
    agent_message,
    bus_card,
    card_summary,
    decision_from_message,
    fetch_card,
    message_data,
    message_text,
    message_urls,
    parse_iso,
    parse_message,
    parts_for,
    rpc_error,
    rpc_result,
    state_from_queue_status,
    state_from_step_status,
    task_to_wire,
    validate_card,
)
from runs import looks_like_run_token
from orchestration import (
    JobSpec,
    StepStatus,
    build_job_state,
    get_ready_steps,
    mark_step_completed,
    mark_step_failed,
    mark_step_running,
    mark_step_skipped,
    validate_job_spec,
)
from store import (
    BACKENDS,
    DEFAULT_DB_PATH,
    STATUS_LEASED,
    STATUS_PENDING,
    STATUS_PUBLISHED,
    create_store,
)

app = Flask(__name__)

# Logging
LOG_LEVEL = os.environ.get("BUS_LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s %(message)s"
)
logger = logging.getLogger("iac-bus")

# Config
BUS_API_TOKEN = os.environ.get("BUS_API_TOKEN", "")
BUS_MAX_MESSAGES = int(os.environ.get("BUS_MAX_MESSAGES", "500"))
BUS_RETENTION_SECONDS = int(os.environ.get("BUS_RETENTION_SECONDS", "3600"))
BUS_QUEUE_LEASE_SECONDS = int(os.environ.get("BUS_QUEUE_LEASE_SECONDS", "60"))
BUS_WAIT_SECONDS_MAX = int(os.environ.get("BUS_WAIT_SECONDS_MAX", "30"))
BUS_LOCK_TTL_DEFAULT_SECONDS = float(os.environ.get("BUS_LOCK_TTL_DEFAULT_SECONDS", "300"))
BUS_LOCK_TTL_MAX_SECONDS = float(os.environ.get("BUS_LOCK_TTL_MAX_SECONDS", "3600"))
BUS_RUN_TTL_DEFAULT_SECONDS = float(os.environ.get("BUS_RUN_TTL_DEFAULT_SECONDS", "3600"))
BUS_RUN_TTL_MAX_SECONDS = float(os.environ.get("BUS_RUN_TTL_MAX_SECONDS", "86400"))
# When true, /bus/* only accepts run tokens; the shared token can still
# register runs and manage them under /agents/*.
BUS_REQUIRE_RUN_TOKEN = os.environ.get("BUS_REQUIRE_RUN_TOKEN", "").strip().lower() in {"1", "true", "yes", "on"}
RUN_MEDIA = ("web", "slack", "ide", "api", "automation", "other")
# A2A interop (a2a_interop.py, docs/A2A_MAPPING.md).
BUS_PUBLIC_URL = os.environ.get("BUS_PUBLIC_URL", "").strip()
BUS_A2A_NAME = os.environ.get("BUS_A2A_NAME", "iac-bus").strip() or "iac-bus"
BUS_A2A_ORG = os.environ.get("BUS_A2A_ORG", "").strip()
BUS_A2A_ALLOW_HOSTS = [h for h in os.environ.get("BUS_A2A_ALLOW_HOSTS", "").split(",") if h.strip()]
GRANT_KEYS = ("skills", "queues", "channels", "lock_paths")
LOCK_EVENTS_CHANNEL = "locks"
LOCK_REASON_MAX_CHARS = 500
BUS_VERSION = os.environ.get("BUS_VERSION", "0.1.0")
BUS_GIT_SHA = os.environ.get("BUS_GIT_SHA", os.environ.get("GITHUB_SHA", ""))
# Storage backend: "memory" (default, state lost on restart) or "sqlite"
# (durable; file named by IAC_BUS_DB). See store.py.
IAC_BUS_STORE = os.environ.get("IAC_BUS_STORE", "memory").strip().lower()
IAC_BUS_DB = os.environ.get("IAC_BUS_DB", DEFAULT_DB_PATH)
if IAC_BUS_STORE not in BACKENDS:
    raise ValueError(f"IAC_BUS_STORE must be one of {BACKENDS}, got {IAC_BUS_STORE!r}")

# Message + agent state. With the default memory backend the store operates
# directly on these two containers, so they remain the live state; with the
# sqlite backend they stay empty and the database holds the records.
_bus_messages = []
_agents = {}
_store = create_store(IAC_BUS_STORE, IAC_BUS_DB, messages=_bus_messages, agents=_agents)
_bus_new_message = threading.Event()

# Orchestration state (in-memory in both backends)
_jobs = {}
_jobs_lock = threading.Lock()

_STARTED_AT = time.time()

# Observability metrics
_metrics_lock = threading.Lock()
_LATENCY_WINDOW = 100
_metrics = {
    "messages_posted": 0,
    "messages_polled": 0,
    "queue_claims": 0,
    "queue_acks": 0,
    "queue_nacks": 0,
    "orchestration_jobs": 0,
    "orchestration_dispatches": 0,
    "locks_acquired": 0,
    "lock_conflicts": 0,
    "locks_renewed": 0,
    "locks_released": 0,
    "locks_expired": 0,
    "runs_registered": 0,
    "runs_ended": 0,
    "a2a_requests": 0,
    "a2a_tasks_created": 0,
    "grant_denials": 0,
    "post_latency_ms": deque(maxlen=_LATENCY_WINDOW),
    "poll_latency_ms": deque(maxlen=_LATENCY_WINDOW),
    "claim_latency_ms": deque(maxlen=_LATENCY_WINDOW),
}


def _bearer_token():
    header = request.headers.get("Authorization", "")
    return header[len("Bearer "):].strip() if header.startswith("Bearer ") else header.strip()


def _require_auth():
    """Authenticate the request and record who is calling.

    * A run token (``iacrun_...``) authenticates as that run: ``g.run`` is
      set and the caller's identity comes from it (see ``_actor_from_request``).
    * Otherwise the shared ``BUS_API_TOKEN`` is required when configured;
      the caller's identity is then the name it sends.
    * With ``BUS_REQUIRE_RUN_TOKEN``, ``/bus/*`` refuses the shared token.
    """
    g.run = None
    token = _bearer_token()
    if looks_like_run_token(token):
        run, reason = _store.runs.authenticate(token)
        if run is None:
            return jsonify({"error": "Unauthorized", "reason": reason}), 401
        g.run = run
        g.auth = "run"
        return None
    if BUS_API_TOKEN and not hmac.compare_digest(token.encode("utf-8"), BUS_API_TOKEN.encode("utf-8")):
        return jsonify({"error": "Unauthorized"}), 401
    g.auth = "shared" if BUS_API_TOKEN else "open"
    if BUS_REQUIRE_RUN_TOKEN and request.path.startswith("/bus/"):
        return jsonify({
            "error": "run token required",
            "hint": "POST /agents/register with the shared token, then send the returned run_token as the bearer token",
        }), 403
    return None


@app.before_request
def _auth_middleware():
    g.request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex
    g.request_started = time.time()
    g.run = None
    g.auth = "none"
    if request.path in ("/health", "/metrics", CARD_PATH):
        return None
    return _require_auth()


@app.after_request
def _request_logging(response):
    started = getattr(g, "request_started", None)
    duration_ms = round((time.time() - started) * 1000, 3) if started is not None else None
    request_id = getattr(g, "request_id", "")
    response.headers["X-Request-ID"] = request_id
    logger.info(
        "request_id=%s method=%s path=%s status=%s duration_ms=%s",
        request_id,
        request.method,
        request.path,
        response.status_code,
        duration_ms,
    )
    return response


def _actor_from_request(claimed):
    """Who is calling. Every handler learns the caller's identity here.

    A caller authenticated with a run token is always its run's ``actor``;
    whatever name it sends (``sender``, ``worker`` or ``agent``) is ignored,
    so one run cannot act as another. A caller using the shared token is
    trusted to name itself, as before run tokens existed.
    """
    run = getattr(g, "run", None)
    if run:
        return run["actor"]
    if not isinstance(claimed, str):
        return None
    claimed = claimed.strip()
    return claimed or None


# ---------------------------------------------------------------------------
# Grants: what a restricted run may touch (deny by default)
# ---------------------------------------------------------------------------


def _run_grants():
    """The caller's grants, or None for an unrestricted caller."""
    run = getattr(g, "run", None)
    return run.get("grants") if run else None


def _grant_allows(kind, value):
    grants = _run_grants()
    if grants is None:
        return True
    return any(fnmatch.fnmatchcase(value, pattern) for pattern in grants.get(kind) or [])


def _grant_allows_path(resource):
    grants = _run_grants()
    if grants is None:
        return True
    for allowed in grants.get("lock_paths") or []:
        try:
            if resource_covers(normalize_resource(allowed), resource):
                return True
        except ValueError:
            continue
    return False


def _forbidden(what, value):
    _metric_inc("grant_denials")
    return jsonify({"error": "forbidden", "reason": f"this run's grants do not allow {what} {value!r}"}), 403


def _normalize_grants(raw, errors):
    """Validate a grants object: lists of fnmatch patterns per kind, deny by default."""
    if raw is None:
        return {key: [] for key in GRANT_KEYS}
    if not isinstance(raw, dict):
        errors.append("grants must be an object")
        return None
    unknown = sorted(set(raw) - set(GRANT_KEYS))
    if unknown:
        errors.append(f"unknown grant kinds: {unknown}; use {list(GRANT_KEYS)}")
    grants = {}
    for key in GRANT_KEYS:
        values = raw.get(key, [])
        if not isinstance(values, list) or not all(isinstance(v, str) and v.strip() for v in values):
            errors.append(f"grants.{key} must be a list of non-empty strings")
            continue
        grants[key] = [v.strip() for v in values]
    for path in grants.get("lock_paths", []):
        try:
            normalize_resource(path)
        except ValueError as exc:
            errors.append(f"grants.lock_paths: {path!r}: {exc}")
    return grants


def _parse_bool(value):
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _metric_inc(name, amount=1):
    with _metrics_lock:
        _metrics[name] = _metrics.get(name, 0) + amount


def _metric_observe(name, value_ms):
    with _metrics_lock:
        _metrics[name].append(float(value_ms))


def _metric_avg(samples):
    if not samples:
        return None
    return round(sum(samples) / len(samples), 3)


def _process_rss_bytes():
    try:
        # Linux: ru_maxrss is kilobytes; prefer current VmRSS from /proc when available.
        with open("/proc/self/status", "r", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("VmRSS:"):
                    parts = line.split()
                    return int(parts[1]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    try:
        usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        # Linux reports KB; macOS reports bytes.
        if os.uname().sysname == "Darwin":
            return int(usage)
        return int(usage) * 1024
    except (OSError, AttributeError, ValueError):
        return None


def _normalize_message_payload(data):
    errors = []
    protocol = data.get("protocol")
    if protocol is not None and not isinstance(protocol, str):
        errors.append("protocol must be string")
    channel = data.get("channel", "default")
    if not isinstance(channel, str):
        errors.append("channel must be string")
    sender = data.get("sender", "agent")
    if not isinstance(sender, str):
        errors.append("sender must be string")
    if "message" not in data:
        errors.append("message required")
        message = None
    else:
        message = data.get("message")
        if message is None:
            errors.append("message required")
        elif isinstance(message, str) and message == "":
            errors.append("message required")
    msg_type = data.get("type", "event")
    if msg_type is not None and not isinstance(msg_type, str):
        errors.append("type must be string")
    conversation_id = data.get("conversation_id")
    if conversation_id is not None and not isinstance(conversation_id, str):
        errors.append("conversation_id must be string")
    reply_to = data.get("reply_to")
    if reply_to is not None and not isinstance(reply_to, str):
        errors.append("reply_to must be string")
    recipient = data.get("recipient")
    if recipient is not None and not isinstance(recipient, str):
        errors.append("recipient must be string")
    group = data.get("group")
    if group is not None and not isinstance(group, str):
        errors.append("group must be string")
    queue = data.get("queue", "")
    if queue is None:
        queue = ""
    if queue and not isinstance(queue, str):
        errors.append("queue must be string")
    if isinstance(queue, str):
        queue = queue.strip()
    priority = data.get("priority", 0)
    if not isinstance(priority, int):
        errors.append("priority must be integer")
    headers = data.get("headers")
    if headers is not None and not isinstance(headers, dict):
        errors.append("headers must be object")
    ttl_seconds = data.get("ttl_seconds")
    if ttl_seconds is not None:
        if not isinstance(ttl_seconds, (int, float)):
            errors.append("ttl_seconds must be number")
        elif ttl_seconds <= 0:
            errors.append("ttl_seconds must be > 0")

    payload = {
        "protocol": protocol,
        "channel": channel,
        "sender": sender,
        "message": message,
        "type": msg_type,
        "conversation_id": conversation_id,
        "reply_to": reply_to,
        "recipient": recipient,
        "group": group,
        "queue": queue or "",
        "priority": priority,
        "headers": headers,
        "ttl_seconds": ttl_seconds,
    }
    return payload, errors


def _build_assignment_payload(job, step):
    return {
        "protocol": "iac-bus/1.1",
        "channel": "orchestration",
        "sender": "orchestrator",
        "message": {
            "job_id": job.job_id,
            "step": step.to_dict(),
        },
        "type": "orchestration.step.assign",
        "queue": step.queue or "orchestration",
        "priority": step.priority,
    }


def _dispatch_ready_steps_locked(job_entry, now=None):
    ready = get_ready_steps(job_entry["spec"], job_entry["state"], now)
    payloads = []
    for step in ready:
        if step.step_id in job_entry["dispatched"]:
            continue
        job_entry["dispatched"].add(step.step_id)
        payloads.append(_build_assignment_payload(job_entry["spec"], step))
    return payloads


def _register_job_locked(job_spec):
    job_entry = {
        "spec": job_spec,
        "state": build_job_state(job_spec),
        "dispatched": set(),
    }
    _jobs[job_spec.job_id] = job_entry
    return _dispatch_ready_steps_locked(job_entry)


def _update_job_step_locked(job_entry, step_id, status, sender):
    now = time.time()
    state = job_entry["state"]
    step_state = state.get_state(step_id)
    status_enum = StepStatus(status)
    if status_enum == StepStatus.RUNNING:
        mark_step_running(state, step_id, now=now, assigned_to=sender)
    elif status_enum == StepStatus.COMPLETED:
        mark_step_completed(state, step_id, now=now)
    elif status_enum == StepStatus.FAILED:
        mark_step_failed(state, step_id, now=now)
    elif status_enum == StepStatus.SKIPPED:
        mark_step_skipped(state, step_id, now=now)
    elif status_enum == StepStatus.TIMED_OUT:
        step_state.mark(StepStatus.TIMED_OUT, now=now)
    else:
        step_state.mark(status_enum, now=now)
    return _dispatch_ready_steps_locked(job_entry, now=now)


def _prepare_orchestration_action(payload):
    msg_type = payload.get("type")
    if msg_type == "orchestration.job":
        message = payload.get("message")
        if not isinstance(message, dict) or "job" not in message:
            return None, ("job payload required", 400)
        try:
            job_spec = JobSpec.from_dict(message["job"])
            validate_job_spec(job_spec)
        except (KeyError, TypeError, ValueError) as exc:
            return None, (str(exc), 400)
        return {"action": "job", "job_spec": job_spec}, None
    if msg_type == "orchestration.step.status":
        message = payload.get("message")
        if not isinstance(message, dict):
            return None, ("message must be object", 400)
        job_id = message.get("job_id")
        step_id = message.get("step_id")
        status = message.get("status")
        if not job_id or not step_id or not status:
            return None, ("job_id, step_id, status required", 400)
        try:
            StepStatus(status)
        except ValueError as exc:
            return None, (str(exc), 400)
        with _jobs_lock:
            job_entry = _jobs.get(job_id)
            if not job_entry:
                return None, ("job not found", 404)
            if step_id not in job_entry["state"].step_states:
                return None, ("step not found", 404)
        return {
            "action": "step_status",
            "job_id": job_id,
            "step_id": step_id,
            "status": status,
        }, None
    return None, None


def _bus_prune(now=None):
    now = now or time.time()
    _store.prune(now, BUS_RETENTION_SECONDS, BUS_MAX_MESSAGES)


def _bus_add_message(payload):
    queue = payload.get("queue", "")
    status = STATUS_PENDING if queue else STATUS_PUBLISHED
    msg = {
        "id": uuid.uuid4().hex,
        "ts": time.time(),
        "protocol": payload.get("protocol") or "iac-bus/1.0",
        "channel": payload["channel"],
        "sender": payload["sender"],
        "message": payload["message"],
        "type": payload["type"],
        "queue": queue,
        "priority": payload["priority"],
        "status": status,
        "leased_by": "",
        "lease_until": 0,
        "lease_id": "",
    }
    for key in ("conversation_id", "reply_to", "recipient", "group", "headers", "ttl_seconds"):
        if payload.get(key) is not None:
            msg[key] = payload[key]
    _store.add_message(msg)
    _bus_new_message.set()
    logger.debug(
        "bus_message_added id=%s channel=%s type=%s queue=%s status=%s sender=%s",
        msg["id"],
        msg["channel"],
        msg.get("type", "event"),
        msg.get("queue", ""),
        msg["status"],
        msg["sender"],
    )
    _bus_prune()
    return msg


def _publish_lock_event(kind, lock):
    """Post a lock lifecycle event to the ``locks`` channel (no lock ids)."""
    if kind == EVENT_EXPIRED:
        _metric_inc("locks_expired")
    _bus_add_message({
        "protocol": "iac-bus/1.1",
        "channel": LOCK_EVENTS_CHANNEL,
        "sender": "iac-bus",
        "message": lock,
        "type": f"lock.{kind}",
        "queue": "",
        "priority": 0,
    })


_store.locks.on_event = _publish_lock_event


def _filter_messages(channel, since_id, limit, include_queue):
    # Expire stale locks first so readers of the `locks` channel see
    # lock.expired events without anyone touching the lock endpoints.
    _store.locks.sweep()
    _bus_prune()
    return _store.list_messages(channel, since_id, include_queue, limit)


def _claim_queue_message(queue, worker, lease_seconds):
    now = time.time()
    _bus_prune(now)
    if lease_seconds is None:
        lease_seconds = BUS_QUEUE_LEASE_SECONDS
    message = _store.claim_queue(queue, worker, lease_seconds, now)
    if message is not None:
        logger.debug(
            "queue_message_claimed queue=%s message_id=%s worker=%s lease_seconds=%s lease_id=%s",
            queue,
            message["id"],
            worker,
            lease_seconds,
            message["lease_id"],
        )
    return message


def _ack_queue_message(queue, message_id, worker, lease_id, requeue):
    _bus_prune()
    message, error = _store.ack_queue(queue, message_id, worker, lease_id, requeue)
    if error is None:
        logger.debug(
            "queue_message_%s queue=%s message_id=%s worker=%s lease_id=%s",
            "nacked" if requeue else "acked",
            queue,
            message_id,
            worker,
            lease_id,
        )
    return message, error


@app.route("/bus/messages", methods=["POST"])
def bus_post_message():
    started = time.time()
    data = request.get_json() or {}
    payload, errors = _normalize_message_payload(data)
    if errors:
        return jsonify({"error": "invalid message", "details": errors}), 400
    actor = _actor_from_request(payload["sender"])
    if actor is not None:
        payload["sender"] = actor
    if not _grant_allows("channels", payload["channel"]):
        return _forbidden("channel", payload["channel"])
    if payload["queue"] and not _grant_allows("queues", payload["queue"]):
        return _forbidden("queue", payload["queue"])
    if _run_grants() is not None and str(payload.get("type") or "").startswith("orchestration."):
        return _forbidden("message type", payload["type"])
    orch_action, orch_error = _prepare_orchestration_action(payload)
    if orch_error:
        error_msg, status_code = orch_error
        return jsonify({"error": error_msg}), status_code
    msg = _bus_add_message(payload)
    _metric_inc("messages_posted")
    logger.debug(
        "bus_post_message accepted id=%s channel=%s type=%s sender=%s",
        msg["id"],
        msg["channel"],
        msg.get("type", "event"),
        msg["sender"],
    )
    if orch_action:
        if orch_action["action"] == "job":
            with _jobs_lock:
                payloads = _register_job_locked(orch_action["job_spec"])
            _metric_inc("orchestration_jobs")
        else:
            with _jobs_lock:
                job_entry = _jobs.get(orch_action["job_id"])
                payloads = _update_job_step_locked(
                    job_entry,
                    orch_action["step_id"],
                    orch_action["status"],
                    payload["sender"],
                )
        for assignment in payloads:
            _bus_add_message(assignment)
        if payloads:
            _metric_inc("orchestration_dispatches", len(payloads))
        logger.debug(
            "orchestration_dispatch processed action=%s payloads_enqueued=%s",
            orch_action["action"],
            len(payloads),
        )
        if orch_action["action"] == "step_status":
            _a2a_on_step_status(orch_action, payload.get("message") or {})
    _a2a_on_message(payload)
    _metric_observe("post_latency_ms", (time.time() - started) * 1000)
    return jsonify({"success": True, "message": msg}), 201


@app.route("/bus/messages", methods=["GET"])
def bus_get_messages():
    started = time.time()
    channel = request.args.get("channel", "")
    if _run_grants() is not None and (not channel or not _grant_allows("channels", channel)):
        return _forbidden("channel", channel or "(all channels)")
    since_id = request.args.get("since_id", "")
    limit = int(request.args.get("limit", "50"))
    include_queue = _parse_bool(request.args.get("include_queue", "false"))
    wait_raw = request.args.get("wait_seconds", "0")
    try:
        wait_seconds = float(wait_raw)
    except (TypeError, ValueError):
        return jsonify({"error": "wait_seconds must be number"}), 400
    if wait_seconds < 0:
        return jsonify({"error": "wait_seconds must be >= 0"}), 400
    if wait_seconds > BUS_WAIT_SECONDS_MAX:
        wait_seconds = float(BUS_WAIT_SECONDS_MAX)
    if limit > 200:
        limit = 200

    deadline = time.time() + wait_seconds if wait_seconds > 0 else None
    while True:
        msgs = _filter_messages(channel, since_id, limit, include_queue)
        if msgs or deadline is None:
            break
        remaining = deadline - time.time()
        if remaining <= 0:
            break
        _bus_new_message.clear()
        # Re-check after clear to avoid missing a notify that raced with clear.
        msgs = _filter_messages(channel, since_id, limit, include_queue)
        if msgs:
            break
        _bus_new_message.wait(timeout=min(remaining, 0.25))

    _metric_inc("messages_polled")
    _metric_observe("poll_latency_ms", (time.time() - started) * 1000)
    logger.debug(
        "bus_get_messages channel=%s since_id=%s limit=%s include_queue=%s wait_seconds=%s returned=%s",
        channel or "*",
        since_id or "",
        limit,
        include_queue,
        wait_seconds,
        len(msgs),
    )
    return jsonify({"messages": msgs})


@app.route("/bus/queues/claim", methods=["POST"])
def bus_claim_queue_message():
    started = time.time()
    data = request.get_json() or {}
    queue = data.get("queue", "")
    worker = _actor_from_request(data.get("worker")) or ""
    lease_seconds = data.get("lease_seconds")
    if not isinstance(queue, str) or not queue.strip():
        return jsonify({"error": "queue required"}), 400
    if not isinstance(worker, str) or not worker.strip():
        return jsonify({"error": "worker required"}), 400
    if lease_seconds is not None:
        if not isinstance(lease_seconds, (int, float)):
            return jsonify({"error": "lease_seconds must be number"}), 400
        if lease_seconds <= 0:
            return jsonify({"error": "lease_seconds must be > 0"}), 400
    if not _grant_allows("queues", queue.strip()):
        return _forbidden("queue", queue.strip())
    msg = _claim_queue_message(queue.strip(), worker.strip(), lease_seconds)
    _metric_observe("claim_latency_ms", (time.time() - started) * 1000)
    if not msg:
        return ("", 204)
    _metric_inc("queue_claims")
    return jsonify({"message": msg})


@app.route("/bus/queues/ack", methods=["POST"])
def bus_ack_queue_message():
    data = request.get_json() or {}
    queue = data.get("queue", "")
    message_id = data.get("message_id", "")
    worker = _actor_from_request(data.get("worker")) or ""
    lease_id = data.get("lease_id", "")
    if not isinstance(queue, str) or not queue.strip():
        return jsonify({"error": "queue required"}), 400
    if not isinstance(message_id, str) or not message_id.strip():
        return jsonify({"error": "message_id required"}), 400
    if not isinstance(worker, str) or not worker.strip():
        return jsonify({"error": "worker required"}), 400
    if not isinstance(lease_id, str) or not lease_id.strip():
        return jsonify({"error": "lease_id required"}), 400
    if not _grant_allows("queues", queue.strip()):
        return _forbidden("queue", queue.strip())
    msg, error = _ack_queue_message(
        queue.strip(),
        message_id.strip(),
        worker.strip(),
        lease_id.strip(),
        requeue=False,
    )
    if error == "not found":
        return jsonify({"error": "message not found"}), 404
    if error:
        return jsonify({"error": error}), 409
    _metric_inc("queue_acks")
    _a2a_on_queue_done(msg, acked=True, requeue=False, result=data.get("result"), reason="")
    return jsonify({"success": True, "message": msg})


@app.route("/bus/queues/nack", methods=["POST"])
def bus_nack_queue_message():
    data = request.get_json() or {}
    queue = data.get("queue", "")
    message_id = data.get("message_id", "")
    worker = _actor_from_request(data.get("worker")) or ""
    lease_id = data.get("lease_id", "")
    requeue = data.get("requeue", True)
    if not isinstance(queue, str) or not queue.strip():
        return jsonify({"error": "queue required"}), 400
    if not isinstance(message_id, str) or not message_id.strip():
        return jsonify({"error": "message_id required"}), 400
    if not isinstance(worker, str) or not worker.strip():
        return jsonify({"error": "worker required"}), 400
    if not isinstance(lease_id, str) or not lease_id.strip():
        return jsonify({"error": "lease_id required"}), 400
    if not isinstance(requeue, bool):
        return jsonify({"error": "requeue must be boolean"}), 400
    reason = data.get("reason") or ""
    if not isinstance(reason, str):
        return jsonify({"error": "reason must be string"}), 400
    if not _grant_allows("queues", queue.strip()):
        return _forbidden("queue", queue.strip())
    msg, error = _ack_queue_message(
        queue.strip(),
        message_id.strip(),
        worker.strip(),
        lease_id.strip(),
        requeue=requeue,
    )
    if error == "not found":
        return jsonify({"error": "message not found"}), 404
    if error:
        return jsonify({"error": error}), 409
    _metric_inc("queue_nacks")
    _a2a_on_queue_done(msg, acked=False, requeue=requeue, result=None, reason=reason)
    return jsonify({"success": True, "message": msg})


@app.route("/bus/orchestration/jobs/<job_id>", methods=["GET"])
def bus_get_orchestration_job(job_id):
    with _jobs_lock:
        entry = _jobs.get(job_id)
        if not entry:
            return jsonify({"error": "job not found"}), 404
        job = entry["spec"].to_dict()
        state = {step_id: step_state.to_dict() for step_id, step_state in entry["state"].step_states.items()}
    return jsonify({"job": job, "state": state})


@app.route("/bus/orchestration/jobs/<job_id>/ready", methods=["GET"])
def bus_get_orchestration_ready(job_id):
    with _jobs_lock:
        entry = _jobs.get(job_id)
        if not entry:
            return jsonify({"error": "job not found"}), 404
        ready = get_ready_steps(entry["spec"], entry["state"])
        ready = [step for step in ready if step.step_id not in entry["dispatched"]]
    return jsonify({"steps": [step.to_dict() for step in ready]})


# ---------------------------------------------------------------------------
# Path locks (locks.py). Agents lock a repo path before editing it.
# ---------------------------------------------------------------------------


def _is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _lock_ttl(data, errors):
    ttl = data.get("ttl_seconds", BUS_LOCK_TTL_DEFAULT_SECONDS)
    if not _is_number(ttl):
        errors.append("ttl_seconds must be number")
        return None
    if ttl <= 0:
        errors.append("ttl_seconds must be > 0")
        return None
    return float(min(ttl, BUS_LOCK_TTL_MAX_SECONDS))


def _lock_id_and_agent(data, errors):
    lock_id = data.get("lock_id")
    if not isinstance(lock_id, str) or not lock_id.strip():
        errors.append("lock_id required")
        lock_id = None
    else:
        lock_id = lock_id.strip()
    agent = _actor_from_request(data.get("agent"))
    if agent is None:
        errors.append("agent required")
    return lock_id, agent


def _lock_error_response(error):
    if error == ERR_NOT_FOUND:
        return jsonify({"error": error}), 404
    return jsonify({"error": error}), 409


@app.route("/bus/locks/acquire", methods=["POST"])
def bus_lock_acquire():
    data = request.get_json(silent=True) or {}
    errors = []
    agent = _actor_from_request(data.get("agent"))
    if agent is None:
        errors.append("agent required")
    resource = repo = None
    try:
        resource = normalize_resource(data.get("resource"))
    except ValueError as exc:
        errors.append(str(exc))
    try:
        repo = normalize_repo(data.get("repo"))
    except ValueError as exc:
        errors.append(str(exc))
    mode = data.get("mode", MODE_EXCLUSIVE)
    if mode not in MODES:
        errors.append("mode must be 'exclusive' or 'shared'")
    ttl = _lock_ttl(data, errors)
    reason = data.get("reason") or ""
    if not isinstance(reason, str):
        errors.append("reason must be string")
    elif len(reason) > LOCK_REASON_MAX_CHARS:
        errors.append(f"reason must be at most {LOCK_REASON_MAX_CHARS} characters")
    wait_seconds = data.get("wait_seconds", 0)
    if not _is_number(wait_seconds):
        errors.append("wait_seconds must be number")
    elif wait_seconds < 0:
        errors.append("wait_seconds must be >= 0")
    if errors:
        return jsonify({"error": "invalid lock request", "details": errors}), 400

    if not _grant_allows_path(resource):
        return _forbidden("lock path", resource)
    wait_seconds = float(min(wait_seconds, BUS_WAIT_SECONDS_MAX))
    lock, conflicts, waited = _store.locks.acquire(
        repo, resource, agent, mode, ttl, reason, wait_seconds
    )
    if lock is None:
        _metric_inc("lock_conflicts")
        first = conflicts[0]
        logger.debug(
            "lock_conflict repo=%s resource=%s agent=%s held_by=%s waited=%s",
            repo, resource, agent, first["agent"], waited,
        )
        return jsonify({
            "error": "lock conflict",
            "repo": repo,
            "resource": resource,
            "requested_mode": mode,
            "held_by": first["agent"],
            "mode": first["mode"],
            "reason": first["reason"],
            "expires_at": first["expires_at"],
            "conflicting_resource": first["resource"],
            "conflicts": conflicts,
            "waited_seconds": waited,
        }), 409
    _metric_inc("locks_acquired")
    logger.debug(
        "lock_acquired repo=%s resource=%s agent=%s mode=%s token=%s",
        repo, resource, agent, mode, lock["fencing_token"],
    )
    body = dict(lock)
    body["waited_seconds"] = waited
    return jsonify(body), 200


@app.route("/bus/locks/renew", methods=["POST"])
def bus_lock_renew():
    data = request.get_json(silent=True) or {}
    errors = []
    lock_id, agent = _lock_id_and_agent(data, errors)
    ttl = _lock_ttl(data, errors)
    if errors:
        return jsonify({"error": "invalid lock request", "details": errors}), 400
    lock, error = _store.locks.renew(lock_id, agent, ttl)
    if error:
        return _lock_error_response(error)
    _metric_inc("locks_renewed")
    return jsonify(lock), 200


@app.route("/bus/locks/release", methods=["POST"])
def bus_lock_release():
    data = request.get_json(silent=True) or {}
    errors = []
    lock_id, agent = _lock_id_and_agent(data, errors)
    if errors:
        return jsonify({"error": "invalid lock request", "details": errors}), 400
    lock, error = _store.locks.release(lock_id, agent)
    if error:
        return _lock_error_response(error)
    _metric_inc("locks_released")
    return jsonify({"success": True, "lock": public_view(lock)}), 200


@app.route("/bus/locks/validate", methods=["POST"])
def bus_lock_validate():
    """Write-boundary check for fencing tokens.

    200 when ``fencing_token`` belongs to a live lock that covers
    ``resource``; 409 otherwise. A pre-commit hook or the MCP adapter calls
    this right before writing so a holder whose lock expired is stopped.
    """
    data = request.get_json(silent=True) or {}
    errors = []
    resource = repo = None
    try:
        resource = normalize_resource(data.get("resource"))
    except ValueError as exc:
        errors.append(str(exc))
    try:
        repo = normalize_repo(data.get("repo"))
    except ValueError as exc:
        errors.append(str(exc))
    token = data.get("fencing_token")
    if not isinstance(token, int) or isinstance(token, bool):
        errors.append("fencing_token must be integer")
    agent = None
    if data.get("agent") is not None:
        agent = _actor_from_request(data.get("agent"))
        if agent is None:
            errors.append("agent must be a non-empty string")
    if errors:
        return jsonify({"error": "invalid lock request", "details": errors}), 400
    valid, reason, lock = _store.locks.validate(repo, resource, token, agent)
    body = {"valid": valid, "reason": reason, "repo": repo, "resource": resource,
            "fencing_token": token, "lock": lock}
    return jsonify(body), (200 if valid else 409)


@app.route("/bus/locks", methods=["GET"])
def bus_lock_list():
    resource_prefix = request.args.get("resource_prefix")
    repo = request.args.get("repo")
    agent = request.args.get("agent")
    errors = []
    if resource_prefix is not None and resource_prefix.strip():
        try:
            resource_prefix = normalize_resource(resource_prefix)
        except ValueError as exc:
            errors.append(str(exc))
    else:
        resource_prefix = None
    if repo is not None:
        try:
            repo = normalize_repo(repo)
        except ValueError as exc:
            errors.append(str(exc))
    if errors:
        return jsonify({"error": "invalid lock query", "details": errors}), 400
    locks = _store.locks.list(repo=repo, resource_prefix=resource_prefix, agent=agent or None)
    return jsonify({"locks": locks, "count": len(locks)})


_HANDLE_RE = re.compile(r"^[A-Za-z0-9._:@/+-]{1,128}$")


def _optional_str(data, key, errors, default=""):
    value = data.get(key, default)
    if value is None:
        return default
    if not isinstance(value, str):
        errors.append(f"{key} must be string")
        return default
    return value.strip()


@app.route("/agents/register", methods=["POST"])
def agents_register():
    """
    Register an agent and start a run for it.

    Returns the agent record (as before) plus a new run: ``run`` (public
    record), ``actor`` (the identity the bus will use for this run) and
    ``run_token`` (shown once). Send the run token as the bearer token on
    later requests and the bus derives the caller from it.

    Only the shared token (or an open bus) can register; a run token cannot
    mint runs. Agent records go through the configured store; runs are in
    memory in both backends.
    """
    if getattr(g, "run", None):
        return jsonify({"error": "run tokens cannot register runs; use the shared token"}), 403
    data = request.get_json(silent=True) or {}
    errors = []
    handle = data.get("handle") or data.get("agent_handle") or "agent"
    if not isinstance(handle, str) or not _HANDLE_RE.match(handle.strip()):
        errors.append("handle must be 1-128 chars of letters, digits and ._:@/+- (no '#')")
    role = _optional_str(data, "role", errors, "worker") or "worker"
    purpose = _optional_str(data, "purpose", errors)
    medium = _optional_str(data, "medium", errors).lower()
    if medium and medium not in RUN_MEDIA:
        errors.append(f"medium must be one of {RUN_MEDIA}")
    session_id = _optional_str(data, "session_id", errors)
    ttl = data.get("run_ttl_seconds", BUS_RUN_TTL_DEFAULT_SECONDS)
    if not _is_number(ttl) or ttl <= 0:
        errors.append("run_ttl_seconds must be a number > 0")
    grants = _normalize_grants(data.get("grants"), errors) if "grants" in data else None
    if errors:
        return jsonify({"error": "invalid register payload", "details": errors}), 400
    handle = handle.strip()
    ttl = float(min(ttl, BUS_RUN_TTL_MAX_SECONDS))

    agent_uuid = uuid.uuid4().hex
    now = time.time()
    record = {
        "agent_uuid": agent_uuid,
        "agent_handle": handle,
        "role": role,
        "purpose": purpose,
        "status": "active",
        "registered_at": now,
        "last_seen_at": now,
        "ephemeral": not _store.durable,
    }
    _store.put_agent(record)
    run, run_token = _store.runs.create(handle, agent_uuid, ttl, role=role, medium=medium, session_id=session_id,
                                        grants=grants)
    _metric_inc("runs_registered")
    logger.debug(
        "agent_registered uuid=%s handle=%s role=%s run_id=%s actor=%s",
        agent_uuid, handle, role, run["run_id"], run["actor"],
    )
    return jsonify({
        "success": True,
        "agent": record,
        "run": run,
        "actor": run["actor"],
        "run_token": run_token,
    }), 201


def _require_run():
    run = getattr(g, "run", None)
    if not run:
        return None, (jsonify({"error": "this endpoint needs a run token as the bearer token"}), 400)
    return run, None


@app.route("/agents/whoami", methods=["GET"])
def agents_whoami():
    run = getattr(g, "run", None)
    return jsonify({
        "auth": getattr(g, "auth", "none"),
        "actor": run["actor"] if run else None,
        "run": run,
    })


@app.route("/agents/heartbeat", methods=["POST"])
def agents_heartbeat():
    """Keep a run alive. Any request with the run token also extends it."""
    run, error = _require_run()
    if error:
        return error
    run = _store.runs.heartbeat(run["run_id"]) or run
    return jsonify({"success": True, "run": run, "last_seen_at": run["last_seen_at"], "expires_at": run["expires_at"]})


@app.route("/agents/runs", methods=["GET"])
def agents_list_runs():
    if _run_grants() is not None:
        return _forbidden("listing", "runs")
    include_ended = _parse_bool(request.args.get("include_ended", "false"))
    runs = _store.runs.list(active_only=not include_ended)
    return jsonify({"runs": runs, "count": len(runs)})


@app.route("/agents/runs/end", methods=["POST"])
def agents_end_run():
    """End a run: its token stops working and its locks are released.

    With a run token, ends the caller's own run. With the shared token,
    ends the run named by ``run_id`` (operator clean-up).
    """
    caller = getattr(g, "run", None)
    data = request.get_json(silent=True) or {}
    if caller:
        run_id = caller["run_id"]
        if data.get("run_id") not in (None, run_id):
            return jsonify({"error": "a run can only end itself"}), 403
    else:
        run_id = data.get("run_id")
        if not isinstance(run_id, str) or not run_id.strip():
            return jsonify({"error": "run_id required"}), 400
        run_id = run_id.strip()
    run = _store.runs.end(run_id)
    if run is None:
        return jsonify({"error": "run not found or not active"}), 404
    released = _store.locks.release_agent(run["actor"])
    _metric_inc("runs_ended")
    if released:
        _metric_inc("locks_released", len(released))
    return jsonify({"success": True, "run": run, "locks_released": [lock["resource"] for lock in released]})


# ---------------------------------------------------------------------------
# A2A interop (a2a_interop.py; design in docs/A2A_MAPPING.md)
# ---------------------------------------------------------------------------


def _a2a_tasks():
    return _store.a2a_tasks


def _a2a_find_for_work_message(message_id):
    """The A2A task a queue item (or a job's assignment item) is doing."""
    if not message_id:
        return None
    record = _a2a_tasks().find(**{"binding.message_id": message_id})
    if record:
        return record
    msg = _store.get_message(message_id)
    body = msg.get("message") if msg else None
    job_id = body.get("job_id") if isinstance(body, dict) else None
    if isinstance(job_id, str) and job_id.startswith("a2a-"):
        return _a2a_tasks().find(**{"binding.job_id": job_id})
    return None


def _a2a_refresh(record):
    """Derive the task's A2A state from the bus (docs/A2A_MAPPING.md §3.3)."""
    if record is None or record["state"] in TERMINAL:
        return record
    binding = record.get("binding") or {}
    derived = record["state"]
    if binding.get("kind") == "queue":
        msg = _store.get_message(binding["message_id"])
        if msg is not None:
            status = msg.get("status")
            if status == "leased" and msg.get("lease_until", 0) <= time.time():
                status = "pending"
            derived = state_from_queue_status(status) or derived
    elif binding.get("kind") == "job":
        with _jobs_lock:
            entry = _jobs.get(binding["job_id"])
            step = entry["state"].step_states.get(binding["step_id"]) if entry else None
            status = step.status.value if step else None
        if status:
            derived = state_from_step_status(status, record.get("canceled", False))
    if record.get("pending_decision") and derived not in TERMINAL:
        derived = INPUT_REQUIRED
    if derived != record["state"]:
        fields = {"state": derived}
        if record["state"] == INPUT_REQUIRED and derived != INPUT_REQUIRED:
            fields["status_message"] = None
        record = _a2a_tasks().update(record["id"], **fields)
    return record


def _a2a_on_queue_done(message, acked, requeue, result, reason):
    """Ack or nack of a queue item: finish the A2A task it carried, if any."""
    headers = (message or {}).get("headers") or {}
    task_id = headers.get("a2a_task_id")
    record = _a2a_tasks().get(task_id) if task_id else None
    if not record or record["state"] in TERMINAL:
        return
    if acked:
        artifacts = list(record.get("artifacts") or [])
        if result is not None:
            artifacts.append({"artifactId": uuid.uuid4().hex, "name": "result", "parts": parts_for(result)})
        _a2a_tasks().update(task_id, state=COMPLETED, artifacts=artifacts, pending_decision=None,
                            status_message=agent_message("Done.", record["context_id"], task_id))
    elif not requeue:
        _a2a_tasks().update(task_id, state=FAILED, pending_decision=None,
                            status_message=agent_message(reason or "The worker gave the task up.",
                                                         record["context_id"], task_id))


def _a2a_on_step_status(action, body):
    """Orchestration step update for an A2A run-job task: keep its result and reason."""
    job_id = action.get("job_id", "")
    if not job_id.startswith("a2a-"):
        return
    record = _a2a_tasks().find(**{"binding.job_id": job_id})
    if not record or record["state"] in TERMINAL:
        return
    if action["status"] == "completed" and body.get("result") is not None:
        artifacts = list(record.get("artifacts") or [])
        artifacts.append({"artifactId": uuid.uuid4().hex, "name": "result", "parts": parts_for(body["result"])})
        _a2a_tasks().update(record["id"], artifacts=artifacts)
    if action["status"] in ("failed", "timed_out") and body.get("reason"):
        _a2a_tasks().update(record["id"], status_message=agent_message(body["reason"], record["context_id"], record["id"]))
    _a2a_refresh(_a2a_tasks().get(record["id"]))


def _a2a_on_message(payload):
    """Decisions on the bus <-> TASK_STATE_INPUT_REQUIRED on A2A tasks."""
    msg_type = payload.get("type")
    envelope = payload.get("message") if isinstance(payload.get("message"), dict) else {}
    conversation_id = payload.get("conversation_id") or envelope.get("conversation_id")
    if msg_type == "request_decision":
        inner = envelope.get("payload") if isinstance(envelope.get("payload"), dict) else {}
        work_id = (payload.get("headers") or {}).get("work_message_id") or (inner.get("metadata") or {}).get("work_message_id")
        record = _a2a_find_for_work_message(work_id)
        if not record or record["state"] in TERMINAL or not conversation_id:
            return
        prompt = inner.get("prompt") or str(envelope or payload.get("message"))
        choices = inner.get("choices") or []
        labels = ", ".join(c.get("label", c.get("id", "")) for c in choices if isinstance(c, dict))
        question = agent_message(prompt + (f"\nOptions: {labels}" if labels else ""), record["context_id"], record["id"])
        question["parts"].append({"data": {"choices": choices, "reply": (
            "Answer with SendMessage on this taskId: text such as approve or deny, or a data part "
            "{choice_id, approved, notes}.")}, "mediaType": "application/json"})
        _a2a_tasks().update(record["id"], state=INPUT_REQUIRED, status_message=question, pending_decision={
            "conversation_id": conversation_id, "channel": payload.get("channel"), "asked_by": payload.get("sender"),
            "prompt": prompt, "choices": choices})
        _a2a_tasks().append(record["id"], "history", question)
    elif msg_type == "provide_decision" and conversation_id:
        record = _a2a_tasks().find(**{"pending_decision.conversation_id": conversation_id})
        if record and record["state"] not in TERMINAL:
            _a2a_tasks().update(record["id"], pending_decision=None, state=WORKING, status_message=None)


def _a2a_owned(task_id, run):
    record = _a2a_tasks().get(task_id) if isinstance(task_id, str) else None
    if record is None or record.get("owner") != run["handle"]:
        raise JsonRpcError(TASK_NOT_FOUND, f"task {task_id!r} not found")
    return _a2a_refresh(record)


def _a2a_default_queue():
    grants = _run_grants()
    if grants is None:
        return DEFAULT_QUEUE
    for pattern in grants.get("queues") or []:
        if not any(ch in pattern for ch in "*?["):
            return pattern
    raise JsonRpcError(PERMISSION_DENIED, "no queue granted to this agent; set metadata.queue")


def _a2a_wait(task_id, config):
    """Block up to BUS_WAIT_SECONDS_MAX for a terminal or interrupted state (decision A)."""
    if config.get("returnImmediately", False):
        return _a2a_refresh(_a2a_tasks().get(task_id))
    deadline = time.time() + BUS_WAIT_SECONDS_MAX
    record = _a2a_refresh(_a2a_tasks().get(task_id))
    while time.time() < deadline and record["state"] not in TERMINAL and record["state"] not in INTERRUPTED:
        time.sleep(0.25)
        record = _a2a_refresh(_a2a_tasks().get(task_id))
    return record


def _a2a_send_message(params, run):
    message = parse_message(params.get("message"))
    config = params.get("configuration") or {}
    if not isinstance(config, dict):
        raise JsonRpcError(INVALID_PARAMS, "configuration must be an object")
    if config.get("taskPushNotificationConfig"):
        raise JsonRpcError(PUSH_NOT_SUPPORTED, "push notifications are not supported")
    if params.get("metadata") is not None and not isinstance(params.get("metadata"), dict):
        raise JsonRpcError(INVALID_PARAMS, "metadata must be an object")
    meta = dict(message.get("metadata") or {})
    meta.update(params.get("metadata") or {})
    actor = run["actor"]
    stored = dict(message, role=ROLE_USER)

    if message.get("taskId"):
        record = _a2a_owned(message["taskId"], run)
        if record["state"] in TERMINAL:
            raise JsonRpcError(UNSUPPORTED_OPERATION, f"task is {record['state']}; terminal tasks cannot take messages")
        _a2a_tasks().append(record["id"], "history", stored)
        pending = record.get("pending_decision")
        if pending:
            answer = decision_from_message(message)
            _bus_add_message({
                "protocol": "iac-agent/1.0", "channel": pending.get("channel") or "decisions", "sender": actor,
                "type": "provide_decision", "conversation_id": pending["conversation_id"], "priority": 0, "queue": "",
                "message": {"kind": "response", "action": "provide_decision", "from": actor,
                            "to": pending.get("asked_by"), "conversation_id": pending["conversation_id"],
                            "payload": answer},
            })
            _a2a_tasks().update(record["id"], pending_decision=None, state=WORKING, status_message=None)
        else:
            _bus_add_message({
                "protocol": "iac-bus/1.1", "channel": f"a2a.task.{record['id']}", "sender": actor,
                "type": "a2a.message", "conversation_id": record["context_id"], "priority": 0, "queue": "",
                "message": {"task_id": record["id"], "text": message_text(message), "data": message_data(message)},
            })
        task_id = record["id"]
    else:
        skill = meta.get("skill") or SKILL_ENQUEUE
        if skill not in SKILLS:
            raise JsonRpcError(INVALID_PARAMS, f"unknown skill {skill!r}; this bus offers {list(SKILLS)}")
        if not _grant_allows("skills", skill):
            _metric_inc("grant_denials")
            raise JsonRpcError(PERMISSION_DENIED, f"this agent may not use skill {skill!r}")
        queue = meta.get("queue") or _a2a_default_queue()
        if not isinstance(queue, str) or not queue.strip():
            raise JsonRpcError(INVALID_PARAMS, "metadata.queue must be a non-empty string")
        if not _grant_allows("queues", queue):
            _metric_inc("grant_denials")
            raise JsonRpcError(PERMISSION_DENIED, f"this agent may not use queue {queue!r}")
        lock_paths = meta.get("lockPaths") or []
        if not isinstance(lock_paths, list):
            raise JsonRpcError(INVALID_PARAMS, "metadata.lockPaths must be an array of repo paths")
        try:
            lock_paths = [normalize_resource(p) for p in lock_paths]
        except ValueError as exc:
            raise JsonRpcError(INVALID_PARAMS, f"metadata.lockPaths: {exc}") from exc
        task_id = uuid.uuid4().hex
        context_id = message.get("contextId") or uuid.uuid4().hex
        text = message_text(message)
        work = {"a2a": {"task_id": task_id, "context_id": context_id, "requested_by": actor},
                "instructions": text, "data": message_data(message), "urls": message_urls(message),
                "lock_paths": lock_paths}
        if skill == SKILL_ENQUEUE:
            item = _bus_add_message({
                "protocol": "iac-bus/1.1", "channel": "a2a", "sender": actor, "type": "a2a.task", "message": work,
                "queue": queue, "priority": 0, "headers": {"a2a_task_id": task_id}, "conversation_id": context_id,
            })
            binding = {"kind": "queue", "message_id": item["id"]}
        else:
            job_id = f"a2a-{task_id}"
            spec = JobSpec.from_dict({"job_id": job_id, "name": (text[:80] or "a2a task"), "steps": [
                {"id": "work", "queue": queue, "title": text[:120] or "a2a task", "metadata": work}]})
            validate_job_spec(spec)
            with _jobs_lock:
                payloads = _register_job_locked(spec)
            for assignment in payloads:
                _bus_add_message(assignment)
            _metric_inc("orchestration_jobs")
            binding = {"kind": "job", "job_id": job_id, "step_id": "work"}
        now = time.time()
        _a2a_tasks().create({
            "id": task_id, "context_id": context_id, "owner": run["handle"], "created_by": actor,
            "skill": skill, "queue": queue, "binding": binding, "state": SUBMITTED, "status_ts": now,
            "status_message": None, "artifacts": [], "history": [stored], "pending_decision": None,
            "canceled": False, "created_at": now,
        })
        _metric_inc("a2a_tasks_created")
    record = _a2a_wait(task_id, config)
    history_length = config.get("historyLength")
    return {"task": task_to_wire(record, history_length if isinstance(history_length, int) else None)}


def _a2a_get_task(params, run):
    record = _a2a_owned(params.get("id"), run)
    history_length = params.get("historyLength")
    return task_to_wire(record, history_length if isinstance(history_length, int) else None)


def _a2a_list_tasks(params, run):
    page_size = params.get("pageSize", 50)
    if not isinstance(page_size, int) or not 1 <= page_size <= 100:
        raise JsonRpcError(INVALID_PARAMS, "pageSize must be an integer from 1 to 100")
    try:
        offset = int(params.get("pageToken") or 0)
    except (TypeError, ValueError) as exc:
        raise JsonRpcError(INVALID_PARAMS, "invalid pageToken") from exc
    status = params.get("status")
    if status is not None and status not in ALL_STATES:
        raise JsonRpcError(INVALID_PARAMS, f"status must be one of {list(ALL_STATES)}")
    after = params.get("statusTimestampAfter")
    try:
        after_ts = parse_iso(after) if after else None
    except (TypeError, ValueError) as exc:
        raise JsonRpcError(INVALID_PARAMS, "statusTimestampAfter must be an ISO 8601 timestamp") from exc
    records = [_a2a_refresh(r) for r in _a2a_tasks().list(owner=run["handle"])]
    records = [r for r in records
               if (not params.get("contextId") or r["context_id"] == params["contextId"])
               and (status is None or r["state"] == status)
               and (after_ts is None or r.get("status_ts", 0) > after_ts)]
    records.sort(key=lambda r: r.get("status_ts", 0), reverse=True)
    page = records[offset:offset + page_size]
    history_length = params.get("historyLength", 0)
    include_artifacts = bool(params.get("includeArtifacts", False))
    return {
        "tasks": [task_to_wire(r, history_length if isinstance(history_length, int) else 0, include_artifacts) for r in page],
        "nextPageToken": str(offset + page_size) if offset + page_size < len(records) else "",
        "pageSize": page_size,
        "totalSize": len(records),
    }


def _a2a_cancel_task(params, run):
    record = _a2a_owned(params.get("id"), run)
    if record["state"] in TERMINAL:
        raise JsonRpcError(TASK_NOT_CANCELABLE, f"task is already {record['state']}")
    binding = record.get("binding") or {}
    leased_by = ""
    if binding.get("kind") == "queue":
        if not _store.delete_message(binding["message_id"], only_pending=True):
            msg = _store.get_message(binding["message_id"])
            leased_by = (msg or {}).get("leased_by", "")
    elif binding.get("kind") == "job":
        with _jobs_lock:
            entry = _jobs.get(binding["job_id"])
            if entry:
                step = entry["state"].step_states.get(binding["step_id"])
                leased_by = step.assigned_to or "" if step else ""
                if step and not step.status.terminal:
                    mark_step_skipped(entry["state"], binding["step_id"])
        for msg in _store.list_messages("orchestration", "", True, 200):
            body = msg.get("message")
            if isinstance(body, dict) and body.get("job_id") == binding["job_id"]:
                _store.delete_message(msg["id"], only_pending=True)
    _a2a_tasks().update(record["id"], state=CANCELED, canceled=True, pending_decision=None,
                        status_message=agent_message("Canceled by the requester.", record["context_id"], record["id"]))
    _bus_add_message({
        "protocol": "iac-agent/1.0", "channel": f"a2a.task.{record['id']}", "sender": run["actor"],
        "type": "cancel_task", "recipient": leased_by or None, "conversation_id": record["context_id"],
        "priority": 0, "queue": "",
        "message": {"kind": "control", "action": "cancel_task", "from": run["actor"], "to": leased_by or None,
                    "payload": {"task_id": record["id"], "reason": "canceled via A2A CancelTask"}},
    })
    return task_to_wire(_a2a_tasks().get(record["id"]))


_A2A_METHODS = {
    "SendMessage": _a2a_send_message,
    "GetTask": _a2a_get_task,
    "ListTasks": _a2a_list_tasks,
    "CancelTask": _a2a_cancel_task,
}


def _a2a_reply(body, status=200):
    resp = jsonify(body)
    resp.status_code = status
    resp.headers[VERSION_HEADER] = A2A_VERSION
    return resp


@app.route(CARD_PATH, methods=["GET"])
def a2a_agent_card():
    """The bus's own Agent Card (public, like /health)."""
    base = BUS_PUBLIC_URL or request.url_root
    card = bus_card(base, BUS_VERSION, name=BUS_A2A_NAME, organization=BUS_A2A_ORG)
    resp = jsonify(card)
    resp.headers["Cache-Control"] = "public, max-age=300"
    return resp


@app.route("/a2a", methods=["POST"])
def a2a_jsonrpc():
    """A2A 1.0 JSON-RPC binding: SendMessage, GetTask, ListTasks, CancelTask."""
    _metric_inc("a2a_requests")
    run = getattr(g, "run", None)
    if not run:
        return _a2a_reply({"error": "A2A calls need an iac-bus run token (POST /agents/a2a/register); "
                                    "the shared bus token is not accepted here"}, 403)
    try:
        body = json.loads(request.get_data(as_text=True) or "")
    except ValueError:
        return _a2a_reply(rpc_error(None, JsonRpcError(PARSE_ERROR, "request body is not valid JSON")))
    if not isinstance(body, dict) or body.get("jsonrpc") != "2.0" or not isinstance(body.get("method"), str) \
            or "id" not in body:
        req_id = body.get("id") if isinstance(body, dict) else None
        return _a2a_reply(rpc_error(req_id, JsonRpcError(INVALID_REQUEST, "expected a JSON-RPC 2.0 request with jsonrpc, id and method")))
    req_id = body["id"]
    version = (request.headers.get(VERSION_HEADER) or request.args.get(VERSION_HEADER) or "0.3").strip()
    if version != A2A_VERSION and not version.startswith(A2A_VERSION + "."):
        return _a2a_reply(rpc_error(req_id, JsonRpcError(
            VERSION_NOT_SUPPORTED, f"A2A version {version!r} is not supported; send the header {VERSION_HEADER}: {A2A_VERSION}",
            {"supported": A2A_VERSION})))
    params = body.get("params") or {}
    if not isinstance(params, dict):
        return _a2a_reply(rpc_error(req_id, JsonRpcError(INVALID_PARAMS, "params must be an object")))
    method = body["method"]
    handler = _A2A_METHODS.get(method)
    if handler is None:
        code = UNSUPPORTED_METHODS.get(method)
        error = JsonRpcError(code, f"{method} is not supported by this bus") if code else \
            JsonRpcError(METHOD_NOT_FOUND, f"unknown method {method!r}")
        return _a2a_reply(rpc_error(req_id, error))
    try:
        result = handler(params, run)
    except JsonRpcError as exc:
        return _a2a_reply(rpc_error(req_id, exc))
    return _a2a_reply(rpc_result(req_id, result))


def _a2a_agent_public(record):
    card = record.get("card") or {}
    return {
        "agent_uuid": record["agent_uuid"], "handle": record["agent_handle"], "card": card_summary(card),
        "card_url": record.get("card_url"), "grants": record.get("grants"),
        "credential_env": record.get("credential_env"), "outbound_queue": record.get("outbound_queue"),
        "registered_at": record.get("registered_at"),
    }


@app.route("/agents/a2a/register", methods=["POST"])
def agents_a2a_register():
    """Register an external A2A agent by its card; grant it roles; mint its run token.

    Shared token (operator) only. The card comes from ``card_url`` (fetched
    with the SSRF guard: https, public addresses, unless the host is in
    BUS_A2A_ALLOW_HOSTS) or inline as ``card``. ``grants`` are deny-by-default
    allowlists. ``credential_env`` names the env var (on the bridge host) that
    holds the token for calling this agent; the value never reaches the bus.
    """
    if getattr(g, "run", None):
        return jsonify({"error": "registering A2A agents needs the shared token, not a run token"}), 403
    data = request.get_json(silent=True) or {}
    errors = []
    card, card_url = data.get("card"), data.get("card_url")
    if bool(card) == bool(card_url):
        errors.append("pass exactly one of card_url or card")
    elif card_url:
        if not isinstance(card_url, str):
            errors.append("card_url must be a string")
        else:
            try:
                card = fetch_card(card_url, BUS_A2A_ALLOW_HOSTS)
            except ValueError as exc:
                errors.append(str(exc))
    else:
        errors.extend(f"card: {e}" for e in validate_card(card))
    grants = _normalize_grants(data.get("grants"), errors)
    handle = data.get("handle")
    if handle is None and isinstance(card, dict) and isinstance(card.get("name"), str):
        handle = "a2a." + re.sub(r"[^a-z0-9._-]+", "-", card["name"].lower()).strip("-")
    if not isinstance(handle, str) or not _HANDLE_RE.match(handle):
        errors.append("handle must be 1-128 chars of letters, digits and ._:@/+- (no '#')")
    credential_env = data.get("credential_env")
    if credential_env is not None and (not isinstance(credential_env, str)
                                       or not re.match(r"^[A-Z][A-Z0-9_]{0,63}$", credential_env)):
        errors.append("credential_env must be an environment variable NAME like PARTNER_AGENT_TOKEN, never a value")
    ttl = data.get("run_ttl_seconds", BUS_RUN_TTL_MAX_SECONDS)
    if not _is_number(ttl) or ttl <= 0:
        errors.append("run_ttl_seconds must be a number > 0")
    if errors:
        return jsonify({"error": "invalid A2A registration", "details": errors}), 400

    now = time.time()
    existing = next((a for a in _store.list_agents()
                     if a.get("kind") == "a2a" and a.get("agent_handle") == handle), None)
    record = dict(existing or {}, agent_uuid=(existing or {}).get("agent_uuid") or uuid.uuid4().hex)
    record.update({
        "agent_handle": handle, "role": "a2a-agent", "kind": "a2a", "purpose": str(card.get("description", ""))[:200],
        "card": card, "card_url": card_url, "grants": grants, "credential_env": credential_env,
        "outbound_queue": f"a2a.{handle}", "status": "active", "last_seen_at": now,
        "registered_at": (existing or {}).get("registered_at", now), "ephemeral": not _store.durable,
    })
    _store.put_agent(record)
    run, run_token = _store.runs.create(handle, record["agent_uuid"], float(min(ttl, BUS_RUN_TTL_MAX_SECONDS)),
                                        role="a2a-agent", medium="api", grants=grants)
    _metric_inc("runs_registered")
    return jsonify({"success": True, "agent": _a2a_agent_public(record), "run": run, "actor": run["actor"],
                    "run_token": run_token, "outbound_queue": record["outbound_queue"],
                    "updated": existing is not None}), (200 if existing else 201)


@app.route("/agents/a2a", methods=["GET"])
def agents_a2a_list():
    if _run_grants() is not None:
        return _forbidden("listing", "A2A agents")
    agents = [_a2a_agent_public(a) for a in _store.list_agents() if a.get("kind") == "a2a"]
    return jsonify({"agents": agents, "count": len(agents)})


@app.route("/health", methods=["GET"])
def health():
    now = time.time()
    messages_retained = _store.message_count()
    pending, leased = _store.queue_counts(now)
    with _jobs_lock:
        jobs_active = len(_jobs)
    rss = _process_rss_bytes()
    payload = {
        "status": "ok",
        "messages": messages_retained,
        "uptime_seconds": round(now - _STARTED_AT, 3),
        "messages_retained": messages_retained,
        "queue_pending_count": pending,
        "queue_leased_count": leased,
        "jobs_active": jobs_active,
        "locks_held": _store.locks.count(),
        "locks_durable": _store.locks.durable,
        "runs_active": _store.runs.active_count(),
        "a2a_tasks": _store.a2a_tasks.count(),
        "require_run_token": BUS_REQUIRE_RUN_TOKEN,
        "version": BUS_VERSION,
        "git_sha": BUS_GIT_SHA or None,
        "process_rss_bytes": rss,
    }
    return jsonify(payload)


@app.route("/metrics", methods=["GET"])
def metrics():
    now = time.time()
    messages_in_memory = _store.message_count()
    _pending, leased = _store.queue_counts(now)
    with _jobs_lock:
        jobs_in_memory = len(_jobs)
    with _metrics_lock:
        counters = {
            "messages_posted": _metrics["messages_posted"],
            "messages_polled": _metrics["messages_polled"],
            "queue_claims": _metrics["queue_claims"],
            "queue_acks": _metrics["queue_acks"],
            "queue_nacks": _metrics["queue_nacks"],
            "orchestration_jobs": _metrics["orchestration_jobs"],
            "orchestration_dispatches": _metrics["orchestration_dispatches"],
            "locks_acquired": _metrics["locks_acquired"],
            "lock_conflicts": _metrics["lock_conflicts"],
            "locks_renewed": _metrics["locks_renewed"],
            "locks_released": _metrics["locks_released"],
            "locks_expired": _metrics["locks_expired"],
            "runs_registered": _metrics["runs_registered"],
            "runs_ended": _metrics["runs_ended"],
            "a2a_requests": _metrics["a2a_requests"],
            "a2a_tasks_created": _metrics["a2a_tasks_created"],
            "grant_denials": _metrics["grant_denials"],
        }
        timers = {
            "post_latency_ms_avg": _metric_avg(list(_metrics["post_latency_ms"])),
            "poll_latency_ms_avg": _metric_avg(list(_metrics["poll_latency_ms"])),
            "claim_latency_ms_avg": _metric_avg(list(_metrics["claim_latency_ms"])),
            "post_latency_ms_samples": len(_metrics["post_latency_ms"]),
            "poll_latency_ms_samples": len(_metrics["poll_latency_ms"]),
            "claim_latency_ms_samples": len(_metrics["claim_latency_ms"]),
        }
    return jsonify({
        "counters": counters,
        "gauges": {
            "messages_in_memory": messages_in_memory,
            "jobs_in_memory": jobs_in_memory,
            "leased_messages": leased,
            "locks_held": _store.locks.count(),
            "runs_active": _store.runs.active_count(),
        },
        "timers": timers,
    })


if __name__ == "__main__":
    host = os.environ.get("BUS_HOST", "0.0.0.0")
    port = int(os.environ.get("BUS_PORT", "8091"))
    logger.info(
        "Starting IAC Bus on %s:%s version=%s git_sha=%s store=%s db=%s",
        host,
        port,
        BUS_VERSION,
        BUS_GIT_SHA or "unknown",
        _store.backend,
        IAC_BUS_DB if _store.durable else "-",
    )
    app.run(host=host, port=port, threaded=True)
