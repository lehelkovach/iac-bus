#!/usr/bin/env bash
# Litmus: parallel nonblocking workers + barrier sync via iac-bus orchestration.
set -euo pipefail

BUS_URL="${BUS_URL:-http://127.0.0.1:8101}"
BUS_API_TOKEN="${BUS_API_TOKEN:-devtoken}"
AUTH="Authorization: Bearer ${BUS_API_TOKEN}"
JOB_ID="litmus-$(date +%s)-$$"
ORCH="agent:cursor.iac-bus.litmus-0@cloud"
WA="agent:cursor.iac-bus.litmus-a@cloud"
WB="agent:cursor.iac-bus.litmus-b@cloud"
BAR="agent:cursor.iac-bus.litmus-barrier@cloud"

pass=0
fail=0
ok() { echo "PASS: $*"; pass=$((pass + 1)); }
bad() { echo "FAIL: $*"; fail=$((fail + 1)); }

json_get() { python3 -c "import json,sys; d=json.load(sys.stdin); print($1)"; }

echo "Litmus target: ${BUS_URL} job_id=${JOB_ID}"

health="$(curl -fsS "${BUS_URL}/health" || true)"
status="$(printf '%s' "${health}" | json_get 'd.get("status","")' || true)"
if [[ "${status}" == "ok" ]]; then ok "health"; else bad "health (${health:-empty})"; fi

# Post job: bootstrap -> (A || B) -> barrier waits for both
post_job="$(curl -fsS -X POST "${BUS_URL}/bus/messages" \
  -H "${AUTH}" -H 'Content-Type: application/json' \
  -d "$(python3 - <<PY
import json
print(json.dumps({
  "type": "orchestration.job",
  "sender": "${ORCH}",
  "channel": "dogfood",
  "message": {"job": {
    "job_id": "${JOB_ID}",
    "name": "litmus-parallel-barrier",
    "steps": [
      {"id": "bootstrap", "queue": "orchestration", "priority": 10},
      {"id": "worker-a", "depends_on": [{"step_id": "bootstrap"}], "parallel_group": "w", "queue": "orchestration", "priority": 5},
      {"id": "worker-b", "depends_on": [{"step_id": "bootstrap"}], "parallel_group": "w", "queue": "orchestration", "priority": 5},
      {"id": "barrier-sync", "wait_for": ["both-workers"], "queue": "orchestration", "priority": 1}
    ],
    "barriers": [{"id": "both-workers", "requires": ["worker-a", "worker-b"], "mode": "all_completed"}]
  }}
}))
PY
)")"
job_msg_id="$(printf '%s' "${post_job}" | json_get 'd["message"]["id"]')"
[[ -n "${job_msg_id}" ]] && ok "posted job ${job_msg_id}" || bad "post job"

claim_and_complete() {
  local worker="$1" expect_step="$2"
  local claim body mid lease sid
  claim="$(curl -fsS -X POST "${BUS_URL}/bus/queues/claim" \
    -H "${AUTH}" -H 'Content-Type: application/json' \
    -d "{\"queue\":\"orchestration\",\"worker\":\"${worker}\",\"lease_seconds\":120}")" || true
  if [[ -z "${claim}" ]]; then
    bad "claim empty for ${worker} expect ${expect_step}"
    return 1
  fi
  mid="$(printf '%s' "${claim}" | json_get 'd.get("id") or d.get("message",{}).get("id","")' 2>/dev/null || true)"
  # claim responses vary: some APIs return message object directly
  mid="$(printf '%s' "${claim}" | python3 -c 'import json,sys;d=json.load(sys.stdin); print(d.get("id") or (d.get("message") or {}).get("id") or "")')"
  lease="$(printf '%s' "${claim}" | python3 -c 'import json,sys;d=json.load(sys.stdin); print(d.get("lease_id") or (d.get("message") or {}).get("lease_id") or "")')"
  sid="$(printf '%s' "${claim}" | python3 -c 'import json,sys;d=json.load(sys.stdin); m=d.get("message",d); step=(m.get("message") or m).get("step") if isinstance(m.get("message") if False else m, dict) else None
# normalize
payload=d
msg=payload.get("message", payload)
inner=msg.get("message", msg) if isinstance(msg, dict) else {}
step=(inner.get("step") if isinstance(inner, dict) else None) or (msg.get("step") if isinstance(msg, dict) else None) or {}
print((step or {}).get("id",""))')"

  # More reliable parse
  read -r mid lease sid < <(printf '%s' "${claim}" | python3 - <<'PY'
import json,sys
d=json.load(sys.stdin)
# Flask returns the message object at top level from claim endpoint — check server
# tests expect response body is the leased message dict with id/lease_id/message fields
mid=d.get("id","")
lease=d.get("lease_id","")
body=d.get("message")
step={}
if isinstance(body, dict):
    step=body.get("step") or {}
print(mid, lease, step.get("id",""))
PY
)

  if [[ "${sid}" != "${expect_step}" ]]; then
    bad "claimed ${sid:-?} want ${expect_step} (worker=${worker})"
    # still try to complete if non-empty?
  else
    ok "claimed ${sid} as ${worker}"
  fi

  curl -fsS -X POST "${BUS_URL}/bus/messages" \
    -H "${AUTH}" -H 'Content-Type: application/json' \
    -d "{\"type\":\"orchestration.step.status\",\"sender\":\"${worker}\",\"channel\":\"dogfood\",\"message\":{\"job_id\":\"${JOB_ID}\",\"step_id\":\"${expect_step}\",\"status\":\"completed\"}}" >/dev/null

  if [[ -n "${mid}" && -n "${lease}" ]]; then
    curl -fsS -X POST "${BUS_URL}/bus/queues/ack" \
      -H "${AUTH}" -H 'Content-Type: application/json' \
      -d "{\"queue\":\"orchestration\",\"worker\":\"${worker}\",\"message_id\":\"${mid}\",\"lease_id\":\"${lease}\"}" >/dev/null || true
  fi
}

# Bootstrap
claim_and_complete "${ORCH}" "bootstrap"

curl -fsS -X POST "${BUS_URL}/bus/messages" \
  -H "${AUTH}" -H 'Content-Type: application/json' \
  -d "{\"type\":\"lifecycle.event\",\"sender\":\"${ORCH}\",\"channel\":\"dogfood\",\"message\":{\"event\":\"litmus-bootstrap-complete\",\"job_id\":\"${JOB_ID}\"}}" >/dev/null
ok "lifecycle bootstrap"

# Parallel workers (sequential claim in this script; barrier still requires both)
claim_and_complete "${WA}" "worker-a"
claim_and_complete "${WB}" "worker-b"

# Barrier should now be dispatchable
claim_and_complete "${BAR}" "barrier-sync"

curl -fsS -X POST "${BUS_URL}/bus/messages" \
  -H "${AUTH}" -H 'Content-Type: application/json' \
  -d "{\"type\":\"lifecycle.event\",\"sender\":\"${BAR}\",\"channel\":\"dogfood\",\"message\":{\"event\":\"litmus-pass\",\"job_id\":\"${JOB_ID}\"}}" >/dev/null
ok "lifecycle litmus-pass"

# Final job state
state="$(curl -fsS "${BUS_URL}/bus/orchestration/jobs/${JOB_ID}" -H "${AUTH}")"
python3 - <<PY
import json,os,sys
state=json.loads('''${state}''')
ss=state.get("state") or {}
bad=False
for step, st in ss.items():
    s=st.get("status")
    print(f"  {step}: {s}")
    if s != "completed":
        bad=True
if bad:
    raise SystemExit(2)
PY
ok "all steps completed"

ready="$(curl -fsS "${BUS_URL}/bus/orchestration/jobs/${JOB_ID}/ready" -H "${AUTH}")"
ready_n="$(printf '%s' "${ready}" | json_get 'len(d.get("steps",[]))')"
if [[ "${ready_n}" == "0" ]]; then ok "ready empty"; else bad "ready still ${ready_n}"; fi

echo
echo "Litmus summary: ${pass} passed, ${fail} failed (job=${JOB_ID})"
[[ "${fail}" -eq 0 ]]
