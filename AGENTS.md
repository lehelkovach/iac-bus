# AGENTS.md — iac-bus

Inter-Agent Communication Bus: Flask HTTP pub/poll + queue leasing for coordinating
agents. Also hosts OCI/dev-VM deploy helpers and an OpenClaw skill.

## Commands

```bash
python3 -m venv venv
./venv/bin/pip install -r requirements.txt -r requirements-dev.txt
BUS_API_TOKEN=devtoken BUS_PORT=8101 ./venv/bin/python server.py
pytest -q
IAC_BUS_URL=http://127.0.0.1:8101 IAC_BUS_TOKEN=devtoken ./scripts/bus_smoke.sh
```

## Branching

Integration tip is **`master`**. Branch from `master`; open PRs with base **`master`**.

## Continuity

- Stable ops: `.AGENT/RUNBOOK.md` when present; otherwise `docs/LOCAL-AND-OCI-SUITUP.md`.
- Cross-session state: uniquely named `.AGENT/handoffs/<task>.md` only when needed.
- Git branches / PRs are the durable record — no shared action log.

## Deploy notes

- Live bus host: `129.153.192.75:8101` (`docs/OCI-LIVE.md`) — **not** port 8091 (OSLO chat).
- Push→deploy: `.github/workflows/dev-deploy.yml` on `master` (needs `KSG_DEV_VM_*` secrets).
- Hotfix default ref: `master`.

## Safety

Never commit tokens. Prefer local Cursor for routine bus work; Cloud only for OCI/SSH.
