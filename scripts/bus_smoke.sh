#!/usr/bin/env bash
# Smoke-test a local IAC Bus: health → post → poll → claim/ack round-trip.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

BUS_HOST="${BUS_HOST:-127.0.0.1}"
BUS_PORT="${BUS_PORT:-18091}"
BUS_URL="${BUS_URL:-http://${BUS_HOST}:${BUS_PORT}}"
BUS_API_TOKEN="${BUS_API_TOKEN:-smoke-token}"
START_SERVER="${START_SERVER:-1}"
VENV_PYTHON="${VENV_PYTHON:-python3}"

auth_header=()
if [[ -n "$BUS_API_TOKEN" ]]; then
  auth_header=(-H "Authorization: Bearer ${BUS_API_TOKEN}")
fi

cleanup() {
  if [[ -n "${SERVER_PID:-}" ]] && kill -0 "$SERVER_PID" 2>/dev/null; then
    kill "$SERVER_PID" 2>/dev/null || true
    wait "$SERVER_PID" 2>/dev/null || true
  fi
}
trap cleanup EXIT

if [[ "$START_SERVER" == "1" ]]; then
  echo "Starting bus at ${BUS_URL} ..."
  BUS_HOST="$BUS_HOST" BUS_PORT="$BUS_PORT" BUS_API_TOKEN="$BUS_API_TOKEN" \
    "$VENV_PYTHON" server.py &
  SERVER_PID=$!
  for _ in $(seq 1 30); do
    if curl -sf "${BUS_URL}/health" >/dev/null 2>&1; then
      break
    fi
    sleep 0.2
  done
fi

echo "== health =="
curl -sf "${BUS_URL}/health" | "$VENV_PYTHON" -m json.tool

echo "== post message =="
post_resp="$(curl -sf -X POST "${BUS_URL}/bus/messages" \
  "${auth_header[@]}" \
  -H "Content-Type: application/json" \
  -d '{"channel":"ops","sender":"smoke","message":"hello-smoke"}')"
echo "$post_resp" | "$VENV_PYTHON" -m json.tool
msg_id="$(echo "$post_resp" | "$VENV_PYTHON" -c "import sys,json; print(json.load(sys.stdin)['message']['id'])")"

echo "== poll messages =="
curl -sf "${BUS_URL}/bus/messages?channel=ops&since_id=${msg_id}" \
  "${auth_header[@]}" | "$VENV_PYTHON" -m json.tool

echo "== queue post + claim/ack =="
queue_resp="$(curl -sf -X POST "${BUS_URL}/bus/messages" \
  "${auth_header[@]}" \
  -H "Content-Type: application/json" \
  -d '{"queue":"work","sender":"smoke","message":{"task":"smoke-task"}}')"
queue_id="$(echo "$queue_resp" | "$VENV_PYTHON" -c "import sys,json; print(json.load(sys.stdin)['message']['id'])")"

claim_resp="$(curl -sf -X POST "${BUS_URL}/bus/queues/claim" \
  "${auth_header[@]}" \
  -H "Content-Type: application/json" \
  -d '{"queue":"work","worker":"smoke-worker","lease_seconds":30}')"
echo "$claim_resp" | "$VENV_PYTHON" -m json.tool
lease_id="$(echo "$claim_resp" | "$VENV_PYTHON" -c "import sys,json; print(json.load(sys.stdin)['message']['lease_id'])")"

curl -sf -X POST "${BUS_URL}/bus/queues/ack" \
  "${auth_header[@]}" \
  -H "Content-Type: application/json" \
  -d "{\"queue\":\"work\",\"worker\":\"smoke-worker\",\"message_id\":\"${queue_id}\",\"lease_id\":\"${lease_id}\"}" \
  | "$VENV_PYTHON" -m json.tool

echo "bus smoke OK (${BUS_URL})"
