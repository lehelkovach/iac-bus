# KSG multi-agent coordination (active)

See canonical contract: `knowshowgo/docs/AGENT-COORDINATION.md` (branch `cursor/agent-coordination-6c58`).

## Local bus (this Cloud env)

```bash
cd /agent/repos/iac-bus
BUS_API_TOKEN=ksg-coord-dev BUS_PORT=8091 python3 server.py   # :8091
```

If `Authorization: Bearer ksg-coord-dev` returns 401, the running process may still have an older token (historically `devtoken`). Inspect the PID environ or restart with `ksg-coord-dev` so slave/master docs stay aligned.

## Seed ownership (master / coordinator)

```bash
export BUS_API_TOKEN=ksg-coord-dev BUS_URL=http://127.0.0.1:8091

# Announce master
curl -sS -X POST "$BUS_URL/bus/messages" \
  -H "Authorization: Bearer $BUS_API_TOKEN" -H "Content-Type: application/json" \
  -d '{"channel":"ops","sender":"agent:cursor.knowshowgo.0@web","type":"ownership","message":{"role":"master","env":"6c58","bcId":"bc-c5804bb7-0c80-498a-a6ed-19d580bf6c58","owns":["ksg-memory-core","osl-agent","api-tokens","topics-spine"]}}'

# Enqueue slave workstreams (slave claims)
for q in work.auth-session work.builder-ui work.flock-barter; do
  curl -sS -X POST "$BUS_URL/bus/messages" \
    -H "Authorization: Bearer $BUS_API_TOKEN" -H "Content-Type: application/json" \
    -d "{\"channel\":\"task.ksg-coord\",\"queue\":\"$q\",\"sender\":\"agent:cursor.knowshowgo.0@web\",\"type\":\"request\",\"priority\":0,\"message\":{\"assign_to\":\"slave-89d8\",\"instruction\":\"claim this queue; ack when PR merged to dev\"}}"
done
```

Slave claim example:

```bash
curl -sS -X POST "$BUS_URL/bus/queues/claim" \
  -H "Authorization: Bearer $BUS_API_TOKEN" -H "Content-Type: application/json" \
  -d '{"queue":"work.auth-session","worker":"agent:cursor.knowshowgo.0-1@web","lease_seconds":3600}'
```

Ack / nack after claim (use leased message `id`):

```bash
curl -sS -X POST "$BUS_URL/bus/queues/ack" \
  -H "Authorization: Bearer $BUS_API_TOKEN" -H "Content-Type: application/json" \
  -d '{"id":"<id>","worker":"agent:cursor.knowshowgo.0-1@web"}'
```

## Conflict avoidance (bus + GitHub)

| Rule | Detail |
|---|---|
| Exclusive queues | Slave claims only `work.auth-session`, `work.builder-ui`, `work.flock-barter`. Master claims `work.ksg-memory-core`, `work.osl-agent`. |
| `rest-api.js` | Slave owns auth mounts while PR #14 is open. Master PRs #12/#13/#15/#16 also touch `rest-api.js` — **do not merge them ahead of #14**; rebase after. |
| Explore #39 | Tests-only — low overlap; safe parallel to auth. |
| Token Viewer | Slave owns Builder UI. Master deeplink PR (token-viewer #3) pauses until slave handoff/ACK. |
| Durable truth | Bus is in-memory. GitHub PRs + `AGENT-COORDINATION.md` win across Cursor envs. |

Channels: `ops` (handoffs/status), `task.ksg-coord` (ownership/progress/conflict scans), `blockers` (rebase pain).

**Note:** Bus state is in-memory per process. Durable truth = GitHub PRs + `AGENT-COORDINATION.md`. Use the bus for live handoffs inside a shared env; across Cursor environments, treat the markdown + PR comments as source of truth.
