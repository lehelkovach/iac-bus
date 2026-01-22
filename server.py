#!/usr/bin/env python3
"""
Inter-Agent Communication Bus (IAC Bus).
"""

import logging
import os
import threading
import time
import uuid

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


def _bus_add_message(channel, sender, message):
    msg = {
        "id": uuid.uuid4().hex,
        "ts": time.time(),
        "channel": channel,
        "sender": sender,
        "message": message
    }
    with _bus_lock:
        _bus_messages.append(msg)
    _bus_prune()
    return msg


@app.route("/bus/messages", methods=["POST"])
def bus_post_message():
    data = request.get_json() or {}
    channel = data.get("channel", "default")
    sender = data.get("sender", "agent")
    message = data.get("message", "")
    if not message:
        return jsonify({"error": "message required"}), 400
    msg = _bus_add_message(channel, sender, message)
    return jsonify({"success": True, "message": msg}), 201


@app.route("/bus/messages", methods=["GET"])
def bus_get_messages():
    channel = request.args.get("channel", "")
    since_id = request.args.get("since_id", "")
    limit = int(request.args.get("limit", "50"))
    if limit > 200:
        limit = 200
    _bus_prune()
    with _bus_lock:
        msgs = list(_bus_messages)
    if channel:
        msgs = [m for m in msgs if m["channel"] == channel]
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
