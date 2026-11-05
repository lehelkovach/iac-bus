#!/usr/bin/env python3
"""
Inter-Agent Communication Bus (IAC Bus).
"""

import logging
import os
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
)
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
    "post_latency_ms": deque(maxlen=_LATENCY_WINDOW),
    "poll_latency_ms": deque(maxlen=_LATENCY_WINDOW),
    "claim_latency_ms": deque(maxlen=_LATENCY_WINDOW),
}


def _require_auth():
    if not BUS_API_TOKEN:
        return None
    token = request.headers.get("Authorization", "").replace("Bearer ", "")
    if token != BUS_API_TOKEN:
        return jsonify({"error": "Unauthorized"}), 401
    return None


@app.before_request
def _auth_middleware():
    g.request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex
    g.request_started = time.time()
    if request.path in ("/health", "/metrics"):
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

    Today this is the name the caller sends (``sender``, ``worker`` or
    ``agent``), trusted as-is under the shared API token. When per-run tokens
    land, this returns the identity bound to the caller's token instead and
    no handler has to change.
    """
    if not isinstance(claimed, str):
        return None
    claimed = claimed.strip()
    return claimed or None


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
    _metric_observe("post_latency_ms", (time.time() - started) * 1000)
    return jsonify({"success": True, "message": msg}), 201


@app.route("/bus/messages", methods=["GET"])
def bus_get_messages():
    started = time.time()
    channel = request.args.get("channel", "")
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


@app.route("/agents/register", methods=["POST"])
def agents_register():
    """
    Agent registration (ACP Stage 3 hook).

    Records go through the configured store: in-memory by default, or SQLite
    when IAC_BUS_STORE=sqlite (then `ephemeral` is false and the record
    survives a restart). Heartbeat, parent/root relationships and durable
    identity (M1) are still TODO.
    """
    data = request.get_json() or {}
    handle = data.get("handle") or data.get("agent_handle") or "agent"
    if not isinstance(handle, str) or not handle.strip():
        return jsonify({"error": "handle required"}), 400
    role = data.get("role", "worker")
    if role is not None and not isinstance(role, str):
        return jsonify({"error": "role must be string"}), 400
    purpose = data.get("purpose", "")
    if purpose is not None and not isinstance(purpose, str):
        return jsonify({"error": "purpose must be string"}), 400

    agent_uuid = uuid.uuid4().hex
    now = time.time()
    record = {
        "agent_uuid": agent_uuid,
        "agent_handle": handle.strip(),
        "role": role or "worker",
        "purpose": purpose or "",
        "status": "active",
        "registered_at": now,
        "last_seen_at": now,
        "ephemeral": not _store.durable,
    }
    _store.put_agent(record)
    logger.debug(
        "agent_registered uuid=%s handle=%s role=%s",
        agent_uuid,
        record["agent_handle"],
        record["role"],
    )
    return jsonify({"success": True, "agent": record}), 201


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
