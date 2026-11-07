#!/usr/bin/env bash
# A2A walkthrough with curl against a running bus (docs/A2A_DEMO.md shows the output).
#   BUS_URL=http://127.0.0.1:8101 BUS_API_TOKEN=devtoken bash scripts/a2a_curl_demo.sh
set -euo pipefail
BUS_URL="${BUS_URL:-http://127.0.0.1:8101}"
OP="Authorization: Bearer ${BUS_API_TOKEN:?set BUS_API_TOKEN (the operator token)}"
J='Content-Type: application/json'
py() { python3 -c "import json,sys; d=json.load(sys.stdin); print($1)"; }
step() { printf '\n== %s\n' "$*"; }

step "1. Discover: the bus's Agent Card (public)"
curl -fsS "${BUS_URL}/.well-known/agent-card.json" \
  | py '{"name": d["name"], "interface": d["supportedInterfaces"][0], "skills": [s["id"] for s in d["skills"]], "auth": list(d["securitySchemes"])}'

step "2. Operator registers an external agent by its card, with deny-by-default grants"
REG="$(curl -fsS -X POST "${BUS_URL}/agents/a2a/register" -H "${OP}" -H "${J}" -d '{
  "card": {"name": "Partner Agent", "description": "Writes release notes.", "version": "1.0.0",
           "supportedInterfaces": [{"url": "https://partner.example/a2a", "protocolBinding": "JSONRPC", "protocolVersion": "1.0"}],
           "capabilities": {}, "defaultInputModes": ["text/plain"], "defaultOutputModes": ["text/plain"],
           "skills": [{"id": "notes", "name": "Release notes", "description": "Drafts release notes.", "tags": ["docs"]}]},
  "grants": {"skills": ["enqueue-work"], "queues": ["partner-inbox"], "channels": ["a2a.partner"], "lock_paths": ["docs"]},
  "credential_env": "PARTNER_AGENT_TOKEN"}')"
echo "${REG}" | py '{"handle": d["agent"]["handle"], "actor": d["actor"], "grants": d["run"]["grants"], "outbound_queue": d["outbound_queue"], "run_token": d["run_token"][:10] + "..."}'
PARTNER="Authorization: Bearer $(echo "${REG}" | py 'd["run_token"]')"
A2A=(-H "${PARTNER}" -H "${J}" -H "A2A-Version: 1.0")

step "3. The partner sends a task (SendMessage, returnImmediately)"
TASK="$(curl -fsS -X POST "${BUS_URL}/a2a" "${A2A[@]}" -d '{"jsonrpc": "2.0", "id": 1, "method": "SendMessage", "params": {
  "message": {"messageId": "m1", "role": "ROLE_USER", "parts": [{"text": "Draft release notes for 0.3.0 in docs/RELEASE.md"}]},
  "configuration": {"returnImmediately": true}, "metadata": {"lockPaths": ["docs/RELEASE.md"]}}}')"
echo "${TASK}" | py '{"id": d["result"]["task"]["id"], "state": d["result"]["task"]["status"]["state"], "queue": d["result"]["task"]["metadata"]["queue"]}'
TASK_ID="$(echo "${TASK}" | py 'd["result"]["task"]["id"]')"

step "4. Grants hold: a queue it was not granted is refused"
curl -fsS -X POST "${BUS_URL}/a2a" "${A2A[@]}" -d '{"jsonrpc": "2.0", "id": 2, "method": "SendMessage", "params": {
  "message": {"messageId": "m2", "role": "ROLE_USER", "parts": [{"text": "Deploy prod"}]}, "configuration": {"returnImmediately": true},
  "metadata": {"queue": "deploys"}}}' | py 'd["error"]'

step "5. A coding agent (run token) claims the work and asks a question"
WORKER="Authorization: Bearer $(curl -fsS -X POST "${BUS_URL}/agents/register" -H "${OP}" -H "${J}" -d '{"handle": "claude.demo.a"}' | py 'd["run_token"]')"
ITEM="$(curl -fsS -X POST "${BUS_URL}/bus/queues/claim" -H "${WORKER}" -H "${J}" -d '{"queue": "partner-inbox", "worker": "x"}')"
ITEM_ID="$(echo "${ITEM}" | py 'd["message"]["id"]')"; LEASE="$(echo "${ITEM}" | py 'd["message"]["lease_id"]')"
curl -fsS -X POST "${BUS_URL}/bus/messages" -H "${WORKER}" -H "${J}" -d "{\"channel\": \"decisions\", \"type\": \"request_decision\",
  \"conversation_id\": \"conv-demo\", \"headers\": {\"work_message_id\": \"${ITEM_ID}\"},
  \"message\": {\"kind\": \"control\", \"action\": \"request_decision\", \"conversation_id\": \"conv-demo\",
               \"payload\": {\"prompt\": \"Include the breaking API change in the notes?\", \"choices\": [{\"id\": \"yes\", \"label\": \"Yes\"}, {\"id\": \"no\", \"label\": \"No\"}]}}}" >/dev/null
curl -fsS -X POST "${BUS_URL}/a2a" "${A2A[@]}" -d "{\"jsonrpc\": \"2.0\", \"id\": 3, \"method\": \"GetTask\", \"params\": {\"id\": \"${TASK_ID}\"}}" \
  | py '{"state": d["result"]["status"]["state"], "question": d["result"]["status"]["message"]["parts"][0]["text"]}'

step "6. The partner answers on the same task; the bus relays it as a decision"
curl -fsS -X POST "${BUS_URL}/a2a" "${A2A[@]}" -d "{\"jsonrpc\": \"2.0\", \"id\": 4, \"method\": \"SendMessage\", \"params\": {
  \"message\": {\"messageId\": \"m3\", \"role\": \"ROLE_USER\", \"taskId\": \"${TASK_ID}\", \"parts\": [{\"text\": \"yes\"}]},
  \"configuration\": {\"returnImmediately\": true}}}" | py 'd["result"]["task"]["status"]["state"]'
curl -fsS "${BUS_URL}/bus/messages?channel=decisions" -H "${OP}" | py '[(m["type"], m["conversation_id"], (m["message"].get("payload") or {})) for m in d["messages"] if m["type"] == "provide_decision"][-1]'

step "7. The worker finishes and acks with a result; the partner sees the artifact"
curl -fsS -X POST "${BUS_URL}/bus/queues/ack" -H "${WORKER}" -H "${J}" -d "{\"queue\": \"partner-inbox\", \"message_id\": \"${ITEM_ID}\",
  \"worker\": \"x\", \"lease_id\": \"${LEASE}\", \"result\": \"Wrote docs/RELEASE.md (0.3.0, includes the breaking change)\"}" >/dev/null
curl -fsS -X POST "${BUS_URL}/a2a" "${A2A[@]}" -d "{\"jsonrpc\": \"2.0\", \"id\": 5, \"method\": \"GetTask\", \"params\": {\"id\": \"${TASK_ID}\", \"historyLength\": 0}}" \
  | py '{"state": d["result"]["status"]["state"], "artifact": d["result"]["artifacts"][0]["parts"][0]["text"]}'

step "8. ListTasks shows only the partner's own tasks"
curl -fsS -X POST "${BUS_URL}/a2a" "${A2A[@]}" -d '{"jsonrpc": "2.0", "id": 6, "method": "ListTasks", "params": {"pageSize": 10}}' \
  | py '{"totalSize": d["result"]["totalSize"], "states": [t["status"]["state"] for t in d["result"]["tasks"]]}'
