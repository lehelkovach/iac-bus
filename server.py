#!/usr/bin/env python3
"""
Inter-Agent Communication Bus (IAC Bus).
"""

import logging
import os
import threading
import time

from flask import Flask, jsonify, request

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
from store import BusStore, create_store

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
BUS_LOCK_LEASE_SECONDS = int(os.environ.get("BUS_LOCK_LEASE_SECONDS", "60"))
BUS_MAX_WAIT_SECONDS = float(os.environ.get("BUS_MAX_WAIT_SECONDS", "30"))
BUS_VERSION = os.environ.get("BUS_VERSION", "0.1.0-dev")
BUS_DB_PATH = os.environ.get("BUS_DB_PATH", os.path.join("data", "iac-bus.db"))

STATUS_PUBLISHED = "published"
STATUS_PENDING = "pending"

# Durable coordination store (SQLite). Override BUS_DB_PATH=:memory: in tests.
_store: BusStore = create_store(BUS_DB_PATH)

# Orchestration state (in-memory; not part of v0.1 durable MVP)
_jobs = {}
_jobs_lock = threading.Lock()


def reset_store(db_path=None):
    """Replace the process store (used by tests)."""
    global _store, BUS_DB_PATH
    try:
        _store.close()
    except Exception:
        pass
    if db_path is not None:
        BUS_DB_PATH = db_path
    _store = create_store(BUS_DB_PATH)
    return _store


def _require_auth():
    if not BUS_API_TOKEN:
        return None
    token = request.headers.get("Authorization", "").replace("Bearer ", "")
    if token != BUS_API_TOKEN:
        return jsonify({"error": "Unauthorized"}), 401
    return None


@app.before_request
def _auth_middleware():
    if request.path == "/health":
        return None
    return _require_auth()


def _parse_bool(value):
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


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
    _store.prune(
        now=now,
        retention_seconds=BUS_RETENTION_SECONDS,
        max_messages=BUS_MAX_MESSAGES,
    )


def _bus_add_message(payload):
    queue = payload.get("queue", "")
    status = STATUS_PENDING if queue else STATUS_PUBLISHED
    msg = _store.add_message(payload, status=status)
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


def _claim_queue_message(queue, worker, lease_seconds):
    now = time.time()
    _bus_prune(now)
    if lease_seconds is None:
        lease_seconds = BUS_QUEUE_LEASE_SECONDS
    msg = _store.claim_queue(queue, worker, lease_seconds, now=now)
    if msg:
        logger.debug(
            "queue_message_claimed queue=%s message_id=%s worker=%s lease_seconds=%s lease_id=%s",
            queue,
            msg["id"],
            worker,
            lease_seconds,
            msg["lease_id"],
        )
    return msg


def _ack_queue_message(queue, message_id, worker, lease_id, requeue):
    _bus_prune()
    msg, error = _store.ack_queue(queue, message_id, worker, lease_id, requeue)
    if error is None and msg is not None:
        action = "nacked" if requeue else "acked"
        logger.debug(
            "queue_message_%s queue=%s message_id=%s worker=%s lease_id=%s",
            action,
            queue,
            message_id,
            worker,
            lease_id,
        )
    return msg, error


@app.route("/bus/messages", methods=["POST"])
def bus_post_message():
    data = request.get_json() or {}
    payload, errors = _normalize_message_payload(data)
    if errors:
        return jsonify({"error": "invalid message", "details": errors}), 400
    orch_action, orch_error = _prepare_orchestration_action(payload)
    if orch_error:
        error_msg, status_code = orch_error
        return jsonify({"error": error_msg}), status_code
    msg = _bus_add_message(payload)
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
        logger.debug(
            "orchestration_dispatch processed action=%s payloads_enqueued=%s",
            orch_action["action"],
            len(payloads),
        )
    return jsonify({"success": True, "message": msg}), 201


@app.route("/bus/messages", methods=["GET"])
def bus_get_messages():
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
    if wait_seconds > BUS_MAX_WAIT_SECONDS:
        wait_seconds = BUS_MAX_WAIT_SECONDS
    if limit > 200:
        limit = 200
    _bus_prune()
    msgs = _store.wait_for_messages(
        channel=channel,
        since_id=since_id,
        include_queue=include_queue,
        limit=limit,
        wait_seconds=wait_seconds,
    )
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
    if not msg:
        return ("", 204)
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
    return jsonify({"success": True, "message": msg})


@app.route("/agents/register", methods=["POST"])
def agents_register():
    data = request.get_json() or {}
    result, err = _store.register_agent(data)
    if err:
        error_msg, status_code, extra = err
        body = {"error": error_msg}
        body.update(extra)
        return jsonify(body), status_code
    status = 201 if result.get("created") else 200
    return jsonify(result), status


@app.route("/agents/heartbeat", methods=["POST"])
def agents_heartbeat():
    data = request.get_json() or {}
    result, err = _store.heartbeat(data)
    if err:
        error_msg, status_code = err
        return jsonify({"error": error_msg}), status_code
    return jsonify(result)


@app.route("/agents/<agent_uuid>", methods=["GET"])
def agents_get(agent_uuid):
    agent = _store.get_agent(agent_uuid)
    if not agent:
        return jsonify({"error": "agent not found"}), 404
    return jsonify({"agent": agent})


@app.route("/bus/locks/acquire", methods=["POST"])
def bus_lock_acquire():
    data = request.get_json() or {}
    resource_key = data.get("resource_key", "")
    holder = data.get("holder", "")
    mode = data.get("mode", "exclusive")
    lease_seconds = data.get("lease_seconds", BUS_LOCK_LEASE_SECONDS)
    metadata = data.get("metadata")
    if not isinstance(resource_key, str) or not resource_key.strip():
        return jsonify({"error": "resource_key required"}), 400
    if not isinstance(holder, str) or not holder.strip():
        return jsonify({"error": "holder required"}), 400
    if not isinstance(lease_seconds, (int, float)):
        return jsonify({"error": "lease_seconds must be number"}), 400
    if metadata is not None and not isinstance(metadata, dict):
        return jsonify({"error": "metadata must be object"}), 400
    lock, error = _store.acquire_lock(
        resource_key=resource_key,
        holder=holder,
        mode=mode if isinstance(mode, str) else "exclusive",
        lease_seconds=float(lease_seconds),
        metadata=metadata,
    )
    if error == "lock held":
        existing = _store.get_lock(resource_key)
        return jsonify({"error": error, "lock": existing}), 409
    if error:
        return jsonify({"error": error}), 400
    return jsonify({"success": True, "lock": lock}), 201


@app.route("/bus/locks/renew", methods=["POST"])
def bus_lock_renew():
    data = request.get_json() or {}
    resource_key = data.get("resource_key", "")
    holder = data.get("holder", "")
    fencing_token = data.get("fencing_token")
    lease_seconds = data.get("lease_seconds", BUS_LOCK_LEASE_SECONDS)
    if not isinstance(resource_key, str) or not resource_key.strip():
        return jsonify({"error": "resource_key required"}), 400
    if not isinstance(holder, str) or not holder.strip():
        return jsonify({"error": "holder required"}), 400
    if not isinstance(fencing_token, int):
        return jsonify({"error": "fencing_token required"}), 400
    if not isinstance(lease_seconds, (int, float)):
        return jsonify({"error": "lease_seconds must be number"}), 400
    lock, error = _store.renew_lock(
        resource_key=resource_key,
        holder=holder,
        fencing_token=fencing_token,
        lease_seconds=float(lease_seconds),
    )
    if error:
        status = 409 if "mismatch" in error or error == "lock not held" else 400
        return jsonify({"error": error}), status
    return jsonify({"success": True, "lock": lock})


@app.route("/bus/locks/release", methods=["POST"])
def bus_lock_release():
    data = request.get_json() or {}
    resource_key = data.get("resource_key", "")
    holder = data.get("holder", "")
    fencing_token = data.get("fencing_token")
    if not isinstance(resource_key, str) or not resource_key.strip():
        return jsonify({"error": "resource_key required"}), 400
    if not isinstance(holder, str) or not holder.strip():
        return jsonify({"error": "holder required"}), 400
    if not isinstance(fencing_token, int):
        return jsonify({"error": "fencing_token required"}), 400
    lock, error = _store.release_lock(
        resource_key=resource_key,
        holder=holder,
        fencing_token=fencing_token,
    )
    if error:
        status = 409 if "mismatch" in error or error == "lock not held" else 400
        return jsonify({"error": error}), status
    return jsonify({"success": True, "lock": lock})


@app.route("/bus/locks", methods=["GET"])
def bus_lock_get():
    resource_key = request.args.get("resource_key", "")
    if not resource_key.strip():
        return jsonify({"error": "resource_key required"}), 400
    lock = _store.get_lock(resource_key)
    if not lock:
        return jsonify({"error": "lock not held"}), 404
    return jsonify({"lock": lock})


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


@app.route("/health", methods=["GET"])
def health():
    return jsonify({
        "status": "ok",
        "version": BUS_VERSION,
        "messages": _store.message_count(),
        "db_path": BUS_DB_PATH,
    })


if __name__ == "__main__":
    host = os.environ.get("BUS_HOST", "0.0.0.0")
    port = int(os.environ.get("BUS_PORT", "8091"))
    logger.info("Starting IAC Bus %s on %s:%s (db=%s)", BUS_VERSION, host, port, BUS_DB_PATH)
    app.run(host=host, port=port, threaded=True)
