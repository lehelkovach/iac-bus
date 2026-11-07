# Agent Task Queue (Takeover Canonical)

Status owner: any active takeover agent  
Last updated: 2026-11-07 UTC  
Purpose: single file a new agent reads to continue work safely.

## Read first
- `AGENTS.md` (hard constraints), then `docs/PROGRESS.md` (ladder, phase facts)
  and `docs/OCI-LIVE.md` (VMs, DNS, deploy paths).
- Local loop: `BUS_PORT=8101 BUS_API_TOKEN=devtoken python3 server.py`, then
  `scripts/bus_smoke.sh` and `pytest -q`. Prove changes with both.
- If a blocker below applies, report it. Do not fake VM, DNS or deploy success.

## Current state in one paragraph
`dev` carries locks, MCP adapter, run tokens, A2A, SQLite durable state and the
agent runner. It runs on `iac-bus-coord-dev-https`
(`https://129-146-105-237.sslip.io`, smoke 20/20). Production VM `iac-bus-6c58`
(`129.153.192.75:8101`) is unchanged and still serves an old build.

## Current Blockers

### B-001: Production VM `iac-bus-6c58` runs an old build
- `/health` returns only `messages` and `status`; no locks, runs or A2A.
- No Run Command plugin on the VM, and the one Run Command IAM policy covers a
  different instance, so the OCI API cannot update it.
- Needs one of: its SSH login key (`IAC_BUS_PROD_KEY` / `KSG_DEV_VM_KEY`; the
  OCI API PEM is not a login key), or an OCI Run Command IAM policy for this
  instance (plus the agent plugin enabled).
- Then run `scripts/deploy-prod-vm.sh` (host `129.153.192.75`).

### B-002: `iac-bus.knowshowgo.com` does not exist
- No DNS record (NXDOMAIN at last check). Owner step: A record
  `iac-bus.knowshowgo.com` -> `129.153.192.75`, TTL 300.
- TLS (Caddy) and NSG 80/443 on `iac-bus-6c58` follow the checklist in
  `docs/OCI-LIVE.md`; both depend on B-001 access.
- Until then use `http://129.153.192.75:8101/health` or the sslip.io dev edge.

### B-003: GitHub Actions are manual-only
- The Actions budget is spent; workflows (`dev-deploy.yml`, `prod-deploy.yml`,
  `oci-provision-dev-vm.yml`) run only on manual dispatch (#27).
- Nothing deploys on push. Deploy with scripts, or the OCI API
  (`scripts/oci_launch_bus_vm.py`), until the budget is restored.
- Run `pytest -q` and smoke locally; CI will not do it for you.

## Next Tasks

### T-001: Unblock and update production (needs B-001)
- Deploy current `dev`/`master` to `iac-bus-6c58`; check `/health` shows the
  ops fields and `/metrics` answers.
- Acceptance: prod smoke passes (`scripts/bus_smoke.sh` against it).

### T-002: DNS + TLS cutover (needs B-001, B-002)
- Follow "DNS cutover checklist" in `docs/OCI-LIVE.md`.
- Acceptance: `curl -fsS https://iac-bus.knowshowgo.com/health` is ok.

### T-003: Promote `dev` to `master`
- Open the PR, run gates locally, tag `vX.Y.Z` per `AGENTS.md`.
- Deploy only after approval; update `VERSION` and `docs/PROGRESS.md`.

### T-004: Orchestration jobs into the SQLite store (L5)
- Job state is still in memory; locks, runs, messages and A2A tasks persist.
- Acceptance: jobs survive a restart; tests cover it.

### T-005: Streamable HTTP MCP on the bus (ID6 remainder)
- Add `POST /mcp` so agents need no local stdio adapter.
- Acceptance: a Claude session connects by URL and locks a path.

### T-006: Registry principals and shared-lock schema (L2 / L4 gaps)
- Add principals to the agent registry (runs, grants already exist).
- Fix `resource_locks.resource_key UNIQUE` in `docs/sql/ACP_V2_SCHEMA.sql` so
  several shared holders fit.

### T-007: L3 tasks, dependency invalidation, provenance (not started)

### T-008: Postgres store, DLQ and event ledger (L5)
- Later; not needed until SQLite limits are hit.

## Notes
- Never print secret values; secret names live in `AGENTS.md`.
- Update `docs/PROGRESS.md` whenever a ladder rung or deploy fact changes.
