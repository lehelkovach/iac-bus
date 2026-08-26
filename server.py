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
BUS_VERSION = os.environ.get("BUS_VERSION", "0.1.0")
BUS_GIT_SHA = os.environ.get("BUS_GIT_SHA", os.environ.get("GITHUB_SHA", ""))

STATUS_PUBLISHED = "published"
STATUS_PENDING = "pending"
STATUS_LEASED = "leased"

# In-memory state
_bus_messages = []
_bus_lock = threading.Lock()
_bus_new_message = threading.Event()

# Orchestration state
_jobs = {}
_jobs_lock = threading.Lock()

# Ephemeral agent registry stub (ACP Stage 3 hook — not durable yet)
# TODO(ACP Stage 2–3): replace with durable agent identity registry (SQLite/Postgres).
_agents = {}
_agents_lock = threading.Lock()

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


def _queue_counts_locked(now=None):
    now = now or time.time()
    pending = 0
    leased = 0
    for message in _bus_messages:
        if not message.get("queue"):
            continue
        status = message.get("status")
        if status == STATUS_LEASED and message.get("lease_until", 0) > now:
            leased += 1
        elif status == STATUS_PENDING or (
            status == STATUS_LEASED and message.get("lease_until", 0) <= now
        ):
            pending += 1
    return pending, leased


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


def _message_is_expired(message, now):
    ttl_seconds = message.get("ttl_seconds")
    if ttl_seconds is not None:
        if now - message["ts"] > ttl_seconds:
            return True
    return now - message["ts"] > BUS_RETENTION_SECONDS


def _release_message(message):
    message["status"] = STATUS_PENDING
    message["leased_by"] = ""
    message["lease_until"] = 0
    message["lease_id"] = ""


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
    with _bus_lock:
        for message in _bus_messages:
            if message.get("status") == STATUS_LEASED and message.get("lease_until", 0) <= now:
                _release_message(message)
        _bus_messages[:] = [m for m in _bus_messages if not _message_is_expired(m, now)]
        if len(_bus_messages) > BUS_MAX_MESSAGES:
            _bus_messages[:] = _bus_messages[-BUS_MAX_MESSAGES:]


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
    with _bus_lock:
        _bus_messages.append(msg)
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


def _filter_messages(channel, since_id, limit, include_queue):
    _bus_prune()
    with _bus_lock:
        msgs = list(_bus_messages)
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


def _claim_queue_message(queue, worker, lease_seconds):
    now = time.time()
    _bus_prune(now)
    if lease_seconds is None:
        lease_seconds = BUS_QUEUE_LEASE_SECONDS
    with _bus_lock:
        for message in _bus_messages:
            if message.get("queue") != queue:
                continue
            if message.get("status") == STATUS_LEASED:
                if message.get("lease_until", 0) > now:
                    continue
                _release_message(message)
            if message.get("status") != STATUS_PENDING:
                continue
            message["status"] = STATUS_LEASED
            message["leased_by"] = worker
            message["lease_until"] = now + lease_seconds
            message["lease_id"] = uuid.uuid4().hex
            logger.debug(
                "queue_message_claimed queue=%s message_id=%s worker=%s lease_seconds=%s lease_id=%s",
                queue,
                message["id"],
                worker,
                lease_seconds,
                message["lease_id"],
            )
            return message
    return None


def _ack_queue_message(queue, message_id, worker, lease_id, requeue):
    _bus_prune()
    with _bus_lock:
        for idx, message in enumerate(_bus_messages):
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
                _release_message(message)
                logger.debug(
                    "queue_message_nacked queue=%s message_id=%s worker=%s lease_id=%s",
                    queue,
                    message_id,
                    worker,
                    lease_id,
                )
                return message, None
            _bus_messages.pop(idx)
            logger.debug(
                "queue_message_acked queue=%s message_id=%s worker=%s lease_id=%s",
                queue,
                message_id,
                worker,
                lease_id,
            )
            return message, None
    return None, "not found"


@app.route("/bus/messages", methods=["POST"])
def bus_post_message():
    started = time.time()
    data = request.get_json() or {}
    payload, errors = _normalize_message_payload(data)
    if errors:
        return jsonify({"error": "invalid message", "details": errors}), 400
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
    worker = data.get("worker", "")
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
    worker = data.get("worker", "")
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
    worker = data.get("worker", "")
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


@app.route("/agents/register", methods=["POST"])
def agents_register():
    """
    Ephemeral agent registration stub (ACP Stage 3 hook).

    TODO(ACP Stage 2–3): persist agent_uuid/handle in SQLite/Postgres registry,
    support heartbeat, parent/root relationships, and durable identity.
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
        "ephemeral": True,
    }
    with _agents_lock:
        _agents[agent_uuid] = record
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
    with _bus_lock:
        messages_retained = len(_bus_messages)
        pending, leased = _queue_counts_locked(now)
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
        "version": BUS_VERSION,
        "git_sha": BUS_GIT_SHA or None,
        "process_rss_bytes": rss,
    }
    return jsonify(payload)


@app.route("/metrics", methods=["GET"])
def metrics():
    now = time.time()
    with _bus_lock:
        messages_in_memory = len(_bus_messages)
        _pending, leased = _queue_counts_locked(now)
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
        },
        "timers": timers,
    })


if __name__ == "__main__":
    host = os.environ.get("BUS_HOST", "0.0.0.0")
    port = int(os.environ.get("BUS_PORT", "8091"))
    logger.info("Starting IAC Bus on %s:%s version=%s git_sha=%s", host, port, BUS_VERSION, BUS_GIT_SHA or "unknown")
    app.run(host=host, port=port, threaded=True)
