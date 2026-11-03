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

# 9) path locks: conflict, fencing check, release, hand-over
LOCK_REPO="${CHANNEL}"
lock_post() {
  # usage: lock_post <path> <json>; prints "<http_code> <body>"
  curl -sS -w '\n%{http_code}' -X POST "${BUS_URL}$1" \
    -H "${AUTH_HEADER}" -H 'Content-Type: application/json' -d "$2"
}
lock_code() { printf '%s' "$1" | tail -n1; }
lock_body() { printf '%s' "$1" | sed '$d'; }

noauth_code="$(curl -sS -o /dev/null -w '%{http_code}' -X POST "${BUS_URL}/bus/locks/acquire" \
  -H 'Content-Type: application/json' -d '{"resource":"src/app.py","agent":"smoke-a"}')"
if [[ "${noauth_code}" == "401" ]]; then
  ok "POST /bus/locks/acquire without token -> 401"
else
  bad "POST /bus/locks/acquire without token expected 401 got ${noauth_code}"
fi

res_a="$(lock_post /bus/locks/acquire "{\"repo\":\"${LOCK_REPO}\",\"resource\":\"src/app.py\",\"agent\":\"smoke-a\",\"ttl_seconds\":30,\"reason\":\"smoke\"}")"
lock_a_id="$(lock_body "${res_a}" | json_get 'd.get("lock_id","")')"
token_a="$(lock_body "${res_a}" | json_get 'd.get("fencing_token","")')"
if [[ "$(lock_code "${res_a}")" == "200" && -n "${lock_a_id}" ]]; then
  ok "POST /bus/locks/acquire -> 200 (fencing_token=${token_a})"
else
  bad "POST /bus/locks/acquire expected 200 got $(lock_code "${res_a}")"
fi

res_b="$(lock_post /bus/locks/acquire "{\"repo\":\"${LOCK_REPO}\",\"resource\":\"src\",\"agent\":\"smoke-b\",\"ttl_seconds\":30}")"
held_by="$(lock_body "${res_b}" | json_get 'd.get("held_by","")')"
if [[ "$(lock_code "${res_b}")" == "409" && "${held_by}" == "smoke-a" ]]; then
  ok "overlapping acquire by another agent -> 409 held_by=smoke-a"
else
  bad "overlapping acquire expected 409 held_by=smoke-a got $(lock_code "${res_b}") ${held_by}"
fi

res_v="$(lock_post /bus/locks/validate "{\"repo\":\"${LOCK_REPO}\",\"resource\":\"src/app.py\",\"fencing_token\":${token_a:-0}}")"
if [[ "$(lock_code "${res_v}")" == "200" ]]; then
  ok "POST /bus/locks/validate live token -> 200"
else
  bad "POST /bus/locks/validate expected 200 got $(lock_code "${res_v}")"
fi

res_r="$(lock_post /bus/locks/release "{\"lock_id\":\"${lock_a_id}\",\"agent\":\"smoke-a\"}")"
res_v2="$(lock_post /bus/locks/validate "{\"repo\":\"${LOCK_REPO}\",\"resource\":\"src/app.py\",\"fencing_token\":${token_a:-0}}")"
res_b2="$(lock_post /bus/locks/acquire "{\"repo\":\"${LOCK_REPO}\",\"resource\":\"src\",\"agent\":\"smoke-b\",\"ttl_seconds\":30}")"
lock_b_id="$(lock_body "${res_b2}" | json_get 'd.get("lock_id","")')"
token_b="$(lock_body "${res_b2}" | json_get 'd.get("fencing_token",0)')"
if [[ "$(lock_code "${res_r}")" == "200" && "$(lock_code "${res_v2}")" == "409" \
      && "$(lock_code "${res_b2}")" == "200" && "${token_b}" -gt "${token_a:-0}" ]]; then
  ok "release -> stale token rejected -> next agent acquires with higher token"
else
  bad "lock hand-over (release=$(lock_code "${res_r}") stale=$(lock_code "${res_v2}") b=$(lock_code "${res_b2}") tokens=${token_a}/${token_b})"
fi
lock_post /bus/locks/release "{\"lock_id\":\"${lock_b_id}\",\"agent\":\"smoke-b\"}" >/dev/null

remaining="$(curl -fsS "${BUS_URL}/bus/locks?repo=${LOCK_REPO}" -H "${AUTH_HEADER}" | json_get 'd.get("count")')"
if [[ "${remaining}" == "0" ]]; then
  ok "GET /bus/locks?repo=... empty after releases"
else
  bad "GET /bus/locks?repo=... expected 0 got ${remaining}"
fi

# 10) run tokens: the bus derives the caller from its token
res_reg="$(lock_post /agents/register '{"handle":"smoke-run","role":"smoke","medium":"automation","run_ttl_seconds":120}')"
run_token="$(lock_body "${res_reg}" | json_get 'd.get("run_token","")')"
run_actor="$(lock_body "${res_reg}" | json_get 'd.get("actor","")')"
if [[ "$(lock_code "${res_reg}")" == "201" && "${run_token}" == iacrun_* && "${run_actor}" == smoke-run#* ]]; then
  ok "POST /agents/register -> 201 run token (actor=${run_actor})"
else
  bad "POST /agents/register expected 201 with run token got $(lock_code "${res_reg}")"
fi
RUN_AUTH="Authorization: Bearer ${run_token}"
res_spoof="$(curl -sS -w '\n%{http_code}' -X POST "${BUS_URL}/bus/locks/acquire" -H "${RUN_AUTH}" \
  -H 'Content-Type: application/json' -d "{\"repo\":\"${LOCK_REPO}\",\"resource\":\"run/x.py\",\"agent\":\"someone-else\"}")"
spoof_agent="$(lock_body "${res_spoof}" | json_get 'd.get("agent","")')"
if [[ "$(lock_code "${res_spoof}")" == "200" && "${spoof_agent}" == "${run_actor}" ]]; then
  ok "run token caller claiming another name is recorded as itself"
else
  bad "derived identity expected agent=${run_actor} got $(lock_code "${res_spoof}") ${spoof_agent}"
fi
res_end="$(curl -sS -w '\n%{http_code}' -X POST "${BUS_URL}/agents/runs/end" -H "${RUN_AUTH}" -H 'Content-Type: application/json' -d '{}')"
ended_locks="$(lock_body "${res_end}" | json_get '",".join(d.get("locks_released",[]))')"
after_code="$(curl -sS -o /dev/null -w '%{http_code}' "${BUS_URL}/bus/locks" -H "${RUN_AUTH}")"
if [[ "$(lock_code "${res_end}")" == "200" && "${ended_locks}" == "run/x.py" && "${after_code}" == "401" ]]; then
  ok "ending a run releases its locks and revokes its token"
else
  bad "run end (code=$(lock_code "${res_end}") released=${ended_locks} after=${after_code})"
fi

echo
echo "Smoke summary: ${pass} passed, ${fail} failed"
if [[ "${fail}" -ne 0 ]]; then
  exit 1
fi
