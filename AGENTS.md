# Agent instructions (iac-bus)

Tip branch: `master`. Prefer local work. Do not touch Cloud/OCI unless the human explicitly asks.

## Ingest order

1. This file (`AGENTS.md`)
2. `docs/LOCAL-AND-OCI-SUITUP.md`
3. `docs/OCI-LIVE.md` or `OCI_DEPLOYMENT.md` — **only** if the task touches deploy/provision

## Hard constraints

- **KeyChain (`key-chain-network`) is not a dependency.** Do not add it, wire it, or assume it.
- Do not change deploy secrets or production/dev VM credential wiring unless asked.
- Do not invent OCI/VM success when secrets or SSH are missing.
- Keep diffs scoped to the requested task.

## Default local loop (M0)

```bash
python3 -m venv venv
./venv/bin/pip install -r requirements.txt -r requirements-dev.txt

BUS_PORT=8101 BUS_API_TOKEN=devtoken ./venv/bin/python server.py
# other terminal:
BUS_PORT=8101 BUS_API_TOKEN=devtoken ./scripts/bus_smoke.sh
./venv/bin/pytest -q
```

On Windows: use `venv\Scripts\python.exe` / `venv\Scripts\pytest.exe`, and run `bus_smoke.sh` via Git Bash.

Acceptance: smoke summary all green; `pytest -q` all green.

## Where to look

| Need | Location |
| --- | --- |
| HTTP API / env vars | `README.md`, `DOCUMENTATION.md` |
| Local + OCI suit-up | `docs/LOCAL-AND-OCI-SUITUP.md` |
| Tests / gates | `docs/TESTING_STRATEGY.md` |
| Takeover task queue | `docs/AGENT_TASKS.md` |
| Protocol / ACP | `docs/ACP_PROTOCOL_V2.md` |
| Dev VM deploy scripts | `scripts/deploy-dev-vm.sh`, `systemd/iac-bus-dev.service` |

## Working rules

- Prove changes with local smoke + pytest before claiming done.
- Do not push, open PRs, or amend published history unless asked.
- If blocked on missing secrets or docs, report the blocker; do not fake progress.
