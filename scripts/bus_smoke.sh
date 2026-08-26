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

# 1) health (no auth) — richer ops fields
health="$(curl -fsS "${BUS_URL}/health" || true)"
status="$(printf '%s' "${health}" | json_get 'd.get("status","")' || true)"
uptime="$(printf '%s' "${health}" | json_get 'd.get("uptime_seconds","")' || true)"
retained="$(printf '%s' "${health}" | json_get 'd.get("messages_retained","")' || true)"
pending="$(printf '%s' "${health}" | json_get 'd.get("queue_pending_count","")' || true)"
leased="$(printf '%s' "${health}" | json_get 'd.get("queue_leased_count","")' || true)"
jobs="$(printf '%s' "${health}" | json_get 'd.get("jobs_active","")' || true)"
if [[ "${status}" == "ok" && -n "${uptime}" && -n "${retained}" && -n "${pending}" && -n "${leased}" && -n "${jobs}" ]]; then
  ok "GET /health (ops fields present)"
else
  bad "GET /health (got: ${health:-<empty>})"
fi

# 1b) metrics endpoint
metrics_code="$(curl -sS -o /tmp/iac-bus-metrics.json -w '%{http_code}' "${BUS_URL}/metrics" || true)"
if [[ "${metrics_code}" == "200" ]]; then
  ok "GET /metrics -> 200"
else
  bad "GET /metrics expected 200 got ${metrics_code}"
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

# 7) wait_seconds timeout returns empty within a bound
wait_channel="smoke-wait-${CHANNEL}"
wait_timing="$(
  WAIT_CHANNEL="${wait_channel}" BUS_URL="${BUS_URL}" BUS_API_TOKEN="${BUS_API_TOKEN}" python3 - <<'PY'
import json, time, urllib.request, os
url = os.environ["BUS_URL"] + "/bus/messages?channel=" + os.environ["WAIT_CHANNEL"] + "&wait_seconds=1"
req = urllib.request.Request(url, headers={"Authorization": "Bearer " + os.environ["BUS_API_TOKEN"]})
started = time.time()
with urllib.request.urlopen(req, timeout=5) as resp:
    body = json.load(resp)
elapsed = time.time() - started
print(json.dumps({"count": len(body.get("messages", [])), "elapsed": round(elapsed, 3)}))
PY
)"
wait_count="$(printf '%s' "${wait_timing}" | json_get 'd.get("count")')"
wait_elapsed="$(printf '%s' "${wait_timing}" | json_get 'd.get("elapsed")')"
if python3 -c "import sys; c=int('${wait_count}'); e=float('${wait_elapsed}'); sys.exit(0 if c==0 and 0.8<=e<=3.0 else 1)"; then
  ok "GET /bus/messages?wait_seconds=1 timeout empty (${wait_elapsed}s)"
else
  bad "wait_seconds timeout expected empty in 0.8-3s got count=${wait_count} elapsed=${wait_elapsed}"
fi

# 8) orchestration happy path: job -> claim assignment -> step.status -> next assignment
ORCH_JOB="smoke-orch-$$"
orch_post="$(curl -fsS \
  -X POST "${BUS_URL}/bus/messages" \
  -H "${AUTH_HEADER}" \
  -H 'Content-Type: application/json' \
  -d "{\"type\":\"orchestration.job\",\"sender\":\"smoke\",\"message\":{\"job\":{\"job_id\":\"${ORCH_JOB}\",\"steps\":[{\"id\":\"design\",\"queue\":\"smoke-orch\"},{\"id\":\"impl\",\"queue\":\"smoke-orch\",\"depends_on\":[{\"step_id\":\"design\"}]}]}}}")"
orch_ok="$(printf '%s' "${orch_post}" | json_get 'd.get("success")')"
claim_body="$(curl -fsS \
  -X POST "${BUS_URL}/bus/queues/claim" \
  -H "${AUTH_HEADER}" \
  -H 'Content-Type: application/json' \
  -d '{"queue":"smoke-orch","worker":"smoke-worker","lease_seconds":30}')"
claim_step="$(printf '%s' "${claim_body}" | json_get 'd.get("message",{}).get("message",{}).get("step",{}).get("id","")')"
claim_msg_id="$(printf '%s' "${claim_body}" | json_get 'd.get("message",{}).get("id","")')"
claim_lease="$(printf '%s' "${claim_body}" | json_get 'd.get("message",{}).get("lease_id","")')"

status_post="$(curl -fsS \
  -X POST "${BUS_URL}/bus/messages" \
  -H "${AUTH_HEADER}" \
  -H 'Content-Type: application/json' \
  -d "{\"type\":\"orchestration.step.status\",\"sender\":\"smoke-worker\",\"message\":{\"job_id\":\"${ORCH_JOB}\",\"step_id\":\"design\",\"status\":\"completed\"}}")"
status_ok="$(printf '%s' "${status_post}" | json_get 'd.get("success")')"

# Ack the first assignment so the next claim can pick up impl
curl -fsS \
  -X POST "${BUS_URL}/bus/queues/ack" \
  -H "${AUTH_HEADER}" \
  -H 'Content-Type: application/json' \
  -d "{\"queue\":\"smoke-orch\",\"worker\":\"smoke-worker\",\"message_id\":\"${claim_msg_id}\",\"lease_id\":\"${claim_lease}\"}" >/dev/null

next_claim="$(curl -fsS \
  -X POST "${BUS_URL}/bus/queues/claim" \
  -H "${AUTH_HEADER}" \
  -H 'Content-Type: application/json' \
  -d '{"queue":"smoke-orch","worker":"smoke-worker","lease_seconds":30}')"
next_step="$(printf '%s' "${next_claim}" | json_get 'd.get("message",{}).get("message",{}).get("step",{}).get("id","")')"

if [[ "${orch_ok}" == "True" && "${claim_step}" == "design" && "${status_ok}" == "True" && "${next_step}" == "impl" ]]; then
  ok "orchestration job -> claim -> status -> next assignment"
else
  bad "orchestration happy path (orch_ok=${orch_ok} claim=${claim_step} status_ok=${status_ok} next=${next_step})"
fi

echo
echo "Smoke summary: ${pass} passed, ${fail} failed"
if [[ "${fail}" -ne 0 ]]; then
  exit 1
fi
