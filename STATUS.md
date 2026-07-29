# STATUS — iac-bus v0.1 coordination MVP

**Branch:** `cursor/iac-bus-v0-1-coordination-ba28`  
**Version:** `0.1.0-dev`  
**Date:** 2026-07-29  
**Role:** Slave under KnowShowGo (iac-bus only)

## Done

| Item | Status |
| --- | --- |
| M1 spawn/terminate Bearer auth + mocked unit tests | ✅ |
| M2 pytest CI (`.github/workflows/test.yml`) + `scripts/bus_smoke.sh` | ✅ |
| SQLite durable store (`store.py`) for messages + queue leases | ✅ |
| Agent registry + heartbeat (`POST /agents/register`, `/agents/heartbeat`, `GET /agents/<id>`) | ✅ |
| Repo/path locks (`/bus/locks/acquire|renew|release`, fencing token) | ✅ |
| `wait_seconds` long-poll on `GET /bus/messages` | ✅ |
| Tests: no double-lease, lock contention/expiry, wait_seconds, persistence | ✅ (`pytest` 40 passed) |
| Smoke green | ✅ |
| `VERSION` / `/health` report `0.1.0-dev` | ✅ |
| Docs: `WHEN-NEEDED-AND-MVP.md`, README endpoints, short STATUS | ✅ |

## Explicitly out of scope (stopped)

- FULL_DEV_PLAN assimilation (task DAG, provenance, Slack bridge, Postgres, OPA)
- OCI prod `:8091` deploy
- Edits to knowshowgo / osl-oc-agent / token-viewer

## How to verify

```bash
pip install -r requirements.txt -r requirements-dev.txt
python3 -m pytest -q
./scripts/bus_smoke.sh
```

## Persistence hook

- Env: `BUS_DB_PATH` (default `data/iac-bus.db`; use `:memory:` in tests)
- Module: `store.BusStore` — swap/replace without rewriting HTTP handlers
