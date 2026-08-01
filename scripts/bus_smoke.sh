#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUS_PORT="${BUS_PORT:-8101}"
BUS_LOG_FILE="${BUS_LOG_FILE:-/tmp/iac-bus-smoke.log}"
STARTED_PID=""

cleanup() {
  if [[ -n "${STARTED_PID}" ]]; then
    kill "${STARTED_PID}" 2>/dev/null || true
    wait "${STARTED_PID}" 2>/dev/null || true
  fi
}
trap cleanup EXIT

if [[ -z "${IAC_BUS_URL:-}" ]]; then
  export BUS_PORT
  export BUS_API_TOKEN="${BUS_API_TOKEN:-smoke-token}"
  export IAC_BUS_URL="http://127.0.0.1:${BUS_PORT}"
  export IAC_BUS_TOKEN="${IAC_BUS_TOKEN:-${BUS_API_TOKEN}}"
  : > "${BUS_LOG_FILE}"
  python3 "${ROOT_DIR}/server.py" >"${BUS_LOG_FILE}" 2>&1 &
  STARTED_PID="$!"
else
  export IAC_BUS_TOKEN="${IAC_BUS_TOKEN:-${BUS_API_TOKEN:-}}"
fi

python3 - <<'PY'
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request


BASE_URL = os.environ["IAC_BUS_URL"].rstrip("/")
TOKEN = os.environ.get("IAC_BUS_TOKEN", "")
STARTED = bool(os.environ.get("BUS_API_TOKEN")) and BASE_URL.startswith("http://127.0.0.1:")


def request(method, path, payload=None, query=None, expect_status=None):
    url = f"{BASE_URL}{path}"
    if query:
        url = f"{url}?{urllib.parse.urlencode(query)}"
    headers = {"Content-Type": "application/json"}
    if TOKEN:
        headers["Authorization"] = f"Bearer {TOKEN}"
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = resp.read()
            status = resp.status
            content_type = resp.headers.get("Content-Type", "")
    except urllib.error.HTTPError as exc:
        data = exc.read()
        status = exc.code
        content_type = exc.headers.get("Content-Type", "") if exc.headers else ""
    if expect_status is not None and status != expect_status:
        raise AssertionError(f"{method} {path} returned {status}, expected {expect_status}: {data!r}")
    if not data:
        return None
    if "application/json" in content_type or data[:1] in (b"{", b"["):
        return json.loads(data.decode("utf-8"))
    return data.decode("utf-8", "replace")


def wait_for_health():
    deadline = time.time() + 10
    last_error = None
    while time.time() < deadline:
        try:
            return request("GET", "/health", expect_status=200)
        except Exception as exc:  # pragma: no cover - shell smoke diagnostic
            last_error = exc
            time.sleep(0.25)
    raise RuntimeError(f"bus did not become healthy: {last_error}")


health = wait_for_health()
assert health["status"] == "ok", health
print(f"health ok: {BASE_URL}")

posted = request(
    "POST",
    "/bus/messages",
    {
        "protocol": "iac-bus/1.0",
        "channel": "smoke",
        "sender": "smoke-agent",
        "type": "progress",
        "message": "smoke progress",
    },
    expect_status=201,
)["message"]
listed = request("GET", "/bus/messages", query={"channel": "smoke"}, expect_status=200)
assert any(msg["id"] == posted["id"] for msg in listed["messages"]), listed
print("post/list ok")

queued = request(
    "POST",
    "/bus/messages",
    {"channel": "smoke", "queue": "smoke-work", "sender": "smoke-agent", "message": {"task": "ack"}},
    expect_status=201,
)["message"]
claimed = request(
    "POST",
    "/bus/queues/claim",
    {"queue": "smoke-work", "worker": "smoke-worker", "lease_seconds": 30},
    expect_status=200,
)["message"]
assert claimed["id"] == queued["id"], claimed
request(
    "POST",
    "/bus/queues/ack",
    {
        "queue": "smoke-work",
        "message_id": claimed["id"],
        "worker": "smoke-worker",
        "lease_id": claimed["lease_id"],
    },
    expect_status=200,
)
print("claim/ack ok")

queued = request(
    "POST",
    "/bus/messages",
    {"channel": "smoke", "queue": "smoke-work", "sender": "smoke-agent", "message": {"task": "nack"}},
    expect_status=201,
)["message"]
claimed = request(
    "POST",
    "/bus/queues/claim",
    {"queue": "smoke-work", "worker": "smoke-worker"},
    expect_status=200,
)["message"]
request(
    "POST",
    "/bus/queues/nack",
    {
        "queue": "smoke-work",
        "message_id": claimed["id"],
        "worker": "smoke-worker",
        "lease_id": claimed["lease_id"],
        "requeue": True,
    },
    expect_status=200,
)
claimed_again = request(
    "POST",
    "/bus/queues/claim",
    {"queue": "smoke-work", "worker": "smoke-worker-2"},
    expect_status=200,
)["message"]
assert claimed_again["id"] == queued["id"], claimed_again
print("nack/reclaim ok")

request(
    "POST",
    "/bus/messages",
    {
        "channel": "session.smoke",
        "sender": "smoke-agent",
        "type": "done",
        "conversation_id": "smoke",
        "message": {"text": "smoke done", "session_id": "smoke", "metadata": {}},
    },
    expect_status=201,
)
print("session convention ok")
print("bus smoke passed")
PY
