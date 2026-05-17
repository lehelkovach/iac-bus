#!/usr/bin/env python3
"""
Inter-Agent Communication Bus (IAC Bus).
"""

import logging
import os
import threading
import time
import uuid
from typing import Any, Dict, Tuple

from flask import Flask, jsonify, request

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

# In-memory state
_bus_messages = []
_bus_lock = threading.Lock()


def _string_field(data: Dict[str, Any], field: str, default: str = "") -> Tuple[str, str]:
    value = data.get(field, default)
    if value is None:
        return default, ""
    if not isinstance(value, str):
        return "", f"{field} must be a string"
    return value, ""


def _metadata_field(data: Dict[str, Any]) -> Tuple[Dict[str, Any], str]:
    value = data.get("metadata", {})
    if value is None:
        return {}, ""
    if not isinstance(value, dict):
        return {}, "metadata must be an object"
    return value, ""


def _message_matches_target(msg: Dict[str, Any], target: str, include_broadcast: bool) -> bool:
    msg_target = msg.get("target", "")
    if msg_target == target:
        return True
    return include_broadcast and not msg_target


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


def _bus_prune(now=None):
    now = now or time.time()
    with _bus_lock:
        _bus_messages[:] = [m for m in _bus_messages if now - m["ts"] <= BUS_RETENTION_SECONDS]
        if len(_bus_messages) > BUS_MAX_MESSAGES:
            _bus_messages[:] = _bus_messages[-BUS_MAX_MESSAGES:]


def _bus_add_message(
    channel,
    sender,
    message,
    *,
    kind="note",
    target="",
    thread_id="",
    reply_to="",
    metadata=None,
):
    msg = {
        "id": uuid.uuid4().hex,
        "ts": time.time(),
        "channel": channel,
        "sender": sender,
        "target": target,
        "kind": kind,
        "thread_id": thread_id,
        "reply_to": reply_to,
        "message": message,
        "metadata": metadata or {},
    }
    with _bus_lock:
        _bus_messages.append(msg)
    _bus_prune()
    return msg


@app.route("/bus/messages", methods=["POST"])
def bus_post_message():
    data = request.get_json() or {}
    if not isinstance(data, dict):
        return jsonify({"error": "JSON body must be an object"}), 400

    fields = {}
    for field, default in (
        ("channel", "default"),
        ("sender", "agent"),
        ("target", ""),
        ("kind", "note"),
        ("thread_id", ""),
        ("reply_to", ""),
    ):
        fields[field], error = _string_field(data, field, default)
        if error:
            return jsonify({"error": error}), 400

    message, error = _string_field(data, "message", "")
    if error:
        return jsonify({"error": error}), 400
    metadata, error = _metadata_field(data)
    if error:
        return jsonify({"error": error}), 400

    if not message:
        return jsonify({"error": "message required"}), 400
    msg = _bus_add_message(message=message, metadata=metadata, **fields)
    return jsonify({"success": True, "message": msg}), 201


@app.route("/bus/messages", methods=["GET"])
def bus_get_messages():
    channel = request.args.get("channel", "")
    target = request.args.get("target", "")
    kind = request.args.get("kind", "")
    thread_id = request.args.get("thread_id", "")
    since_id = request.args.get("since_id", "")
    include_broadcast = request.args.get("include_broadcast", "true").lower() not in (
        "0",
        "false",
        "no",
    )
    try:
        limit = int(request.args.get("limit", "50"))
    except ValueError:
        return jsonify({"error": "limit must be an integer"}), 400
    if limit < 1:
        return jsonify({"error": "limit must be greater than 0"}), 400
    if limit > 200:
        limit = 200
    _bus_prune()
    with _bus_lock:
        msgs = list(_bus_messages)
    if channel:
        msgs = [m for m in msgs if m["channel"] == channel]
    if target:
        msgs = [m for m in msgs if _message_matches_target(m, target, include_broadcast)]
    if kind:
        msgs = [m for m in msgs if m.get("kind", "") == kind]
    if thread_id:
        msgs = [m for m in msgs if m.get("thread_id", "") == thread_id]
    if since_id:
        try:
            idx = next(i for i, m in enumerate(msgs) if m["id"] == since_id)
            msgs = msgs[idx + 1:]
        except StopIteration:
            pass
    return jsonify({"messages": msgs[-limit:]})


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "messages": len(_bus_messages)})


if __name__ == "__main__":
    host = os.environ.get("BUS_HOST", "0.0.0.0")
    port = int(os.environ.get("BUS_PORT", "8091"))
    logger.info("Starting IAC Bus on %s:%s", host, port)
    app.run(host=host, port=port, threaded=True)
