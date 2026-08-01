# IAC Bus — local + OCI suit-up

**Goal:** develop the bus on a laptop (Cursor local), hot-deploy from push to an OCI
dev VM, dogfood inter-agent messages — without burning Cloud Agent quota on routine work.

**Integration tip:** `master` (no `dev` branch yet). Branch from `master`, PR into `master`.

---

## A. Local laptop (primary)

```bash
git clone https://github.com/lehelkovach/iac-bus.git
cd iac-bus
python3 -m venv venv
./venv/bin/pip install -r requirements.txt -r requirements-dev.txt
BUS_API_TOKEN=devtoken BUS_PORT=8101 ./venv/bin/python server.py
# other terminal:
export IAC_BUS_URL=http://127.0.0.1:8101 IAC_BUS_TOKEN=devtoken
./scripts/bus_smoke.sh
pytest -q
```

OpenClaw skill sources: `skills/openclaw/` (+ `docs/OPENCLAW_SKILL.md`).
Point a second local agent / curl loop at the same URL for two-party dogfood:

```bash
# agent-a
curl -sS -X POST "$IAC_BUS_URL/bus/messages" \
  -H "Authorization: Bearer $IAC_BUS_TOKEN" -H "Content-Type: application/json" \
  -d '{"channel":"ops","sender":"agent-a","message":"ready","type":"status"}'
# agent-b poll
curl -sS "$IAC_BUS_URL/bus/messages?channel=ops" \
  -H "Authorization: Bearer $IAC_BUS_TOKEN"
```

---

## B. OCI hot-deploy from push

### Two hosts (do not collide ports)

| Role | Host | Bus port | Notes |
|---|---|---|---|
| **KSG / OSLO prod** | `129.153.118.145` (`ksg-main`) | keep free | Agent chat `:8091`, Slack, KSG — **do not** bind bus to 8091 here |
| **IAC Bus live/dev** | `129.153.192.75` (`iac-bus-6c58`) | **8101** | See `docs/OCI-LIVE.md` |

### GitHub Actions (`dev-deploy.yml`)

On push to `master` (and optional `dev`): pytest → `scripts/deploy-dev-vm.sh`.

**Repo secrets required** (GitHub → Settings → Secrets → Actions):

| Secret | Purpose |
|---|---|
| `KSG_DEV_VM_HOST` | Target host (recommend `129.153.192.75` for bus) |
| `KSG_DEV_VM_USER` | `ubuntu` |
| `KSG_DEV_VM_KEY` | Private SSH key |
| `KSG_DEV_VM_PORT` | optional, default 22 |
| `KSG_DEV_VM_APP_DIR` | optional remote dir |
| `BUS_API_TOKEN` | Bearer for the unit |

Workflow skips deploy (exit 0) if host/user/key are empty — tests still run.

**Port on deploy script:** set `DEV_BUS_PORT=8101` in the workflow env when targeting
`iac-bus-6c58` (default in workflow may still be 8091 for older KSG_DEV_VM layouts —
override via secret/env when wiring).

### Manual redeploy (no Actions)

```bash
ssh -i ~/.ssh/oci_console ubuntu@129.153.192.75
cd ~/iac-bus && git fetch origin && git checkout master && git pull --ff-only
sudo env BUS_PORT=8101 ./deploy.sh   # preserves /etc/iac-bus/iac-bus.env token
sudo systemctl restart iac-bus
curl -sS http://127.0.0.1:8101/health
```

Hotfix scripts default `REF=master` (`scripts/hotfix-pull.sh`).

---

## C. Dogfood inter-agent communication

1. **Local first** — two curl/Python clients or OpenClaw skill against `127.0.0.1:8101`.
2. **Live bus** — `IAC_BUS_URL=http://129.153.192.75:8101` + token from VM env; `./scripts/bus_smoke.sh`.
3. **OSLO** — today only optional `IAC_BUS_URL` health probe (`stack_health.mjs`). Next:
   wire claim/ack tools or load `skills/openclaw` — product Gate A still outranks bus features.
4. **Spawn helpers** — `scripts/spawn-cursor-agents.py` (prefer Bearer-auth fix from open PRs
   before relying on it).

---

## D. Cloud Agent budget

Use Cloud only for: OCI SSH/deploy secrets, live smoke against `129.153.192.75`, or
broken Actions. Routine bus code + pytest → **local Cursor**. Continuity = git PR + this
doc + `docs/OCI-LIVE.md`.
