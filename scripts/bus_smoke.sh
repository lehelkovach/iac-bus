#!/usr/bin/env bash
# Local M0 smoke against a running iac-bus instance.
set -euo pipefail

BUS_PORT="${BUS_PORT:-8101}"
BUS_API_TOKEN="${BUS_API_TOKEN:-devtoken}"
BUS_URL="${BUS_URL:-http://127.0.0.1:${BUS_PORT}}"
CHANNEL="smoke-$(date +%s)-$$"
AUTH_HEADER="Authorization: Bearer ${BUS_API_TOKEN}"

pass=0
fail=0

ok() {
  echo "PASS: $*"
  pass=$((pass + 1))
}

bad() {
  echo "FAIL: $*"
  fail=$((fail + 1))
}

json_get() {
  # usage: json_get <python-expr-using-d>
  python3 -c "import json,sys; d=json.load(sys.stdin); print($1)"
}

need_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "Missing required command: $1" >&2
    exit 1
  fi
}

need_cmd curl
need_cmd python3

echo "Smoke target: ${BUS_URL}"

# 1) health (no auth)
health="$(curl -fsS "${BUS_URL}/health" || true)"
status="$(printf '%s' "${health}" | json_get 'd.get("status","")' || true)"
if [[ "${status}" == "ok" ]]; then
  ok "GET /health"
else
  bad "GET /health (got: ${health:-<empty>})"
fi

# 2) post without token must 401 when token configured
code="$(curl -sS -o /dev/null -w '%{http_code}' \
  -X POST "${BUS_URL}/bus/messages" \
  -H 'Content-Type: application/json' \
  -d "{\"channel\":\"${CHANNEL}\",\"sender\":\"smoke\",\"message\":\"no-auth\"}")"
if [[ "${code}" == "401" ]]; then
  ok "POST /bus/messages without token -> 401"
else
  bad "POST /bus/messages without token expected 401 got ${code}"
fi

# 3) post with token
post_body="$(curl -fsS \
  -X POST "${BUS_URL}/bus/messages" \
  -H "${AUTH_HEADER}" \
  -H 'Content-Type: application/json' \
  -d "{\"channel\":\"${CHANNEL}\",\"sender\":\"smoke\",\"message\":\"hello-m0\"}")"
msg_id="$(printf '%s' "${post_body}" | json_get 'd["message"]["id"]')"
if [[ -n "${msg_id}" ]]; then
  ok "POST /bus/messages with token (id=${msg_id})"
else
  bad "POST /bus/messages with token"
fi

# 4) poll by channel
poll_body="$(curl -fsS \
  "${BUS_URL}/bus/messages?channel=${CHANNEL}" \
  -H "${AUTH_HEADER}")"
count="$(printf '%s' "${poll_body}" | json_get 'len(d.get("messages",[]))')"
if [[ "${count}" -ge 1 ]]; then
  ok "GET /bus/messages?channel=... (${count} msg)"
else
  bad "GET /bus/messages?channel=... expected >=1 got ${count}"
fi

# 5) since_id excludes the posted message
since_body="$(curl -fsS \
  "${BUS_URL}/bus/messages?channel=${CHANNEL}&since_id=${msg_id}" \
  -H "${AUTH_HEADER}")"
since_count="$(printf '%s' "${since_body}" | json_get 'len(d.get("messages",[]))')"
if [[ "${since_count}" == "0" ]]; then
  ok "GET /bus/messages?since_id=... empty"
else
  bad "GET /bus/messages?since_id=... expected 0 got ${since_count}"
fi

# 6) claim empty queue -> 204
claim_code="$(curl -sS -o /dev/null -w '%{http_code}' \
  -X POST "${BUS_URL}/bus/queues/claim" \
  -H "${AUTH_HEADER}" \
  -H 'Content-Type: application/json' \
  -d '{"queue":"smoke-empty","worker":"smoke","lease_seconds":30}')"
if [[ "${claim_code}" == "204" ]]; then
  ok "POST /bus/queues/claim empty -> 204"
else
  bad "POST /bus/queues/claim empty expected 204 got ${claim_code}"
fi

echo
echo "Smoke summary: ${pass} passed, ${fail} failed"
if [[ "${fail}" -ne 0 ]]; then
  exit 1
fi
