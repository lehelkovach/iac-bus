# Agent Task Queue (Takeover Canonical)

Status owner: any active takeover agent  
Last updated: 2026-08-28 UTC  
Purpose: single file a new agent reads to continue work safely.

**TDD process:** [`docs/TDD-LADDER.md`](./TDD-LADDER.md) · canonical
`knowshowgo/docs/TDD-LADDER.md`. Influence A–F do **not** wait on this bus.
Next stack merge is KSG composition S1, not L2.

## Progress pointer
See `docs/PROGRESS.md` (this `dev` tip) and `.AGENT/CONTINUITY.md`.

## Current Blocker

### B-001: OCI provisioning secrets not injected in runtime
- Severity: blocker
- Affects: `scripts/provision-oci-dev-vm.py` and OCI auto-provision workflow
- Missing env vars in active cloud agent sessions:
  - `OCI_TENANCY_OCID`
  - `OCI_USER_OCID`
  - `OCI_FINGERPRINT`
  - `OCI_REGION`
  - `OCI_COMPARTMENT_OCID`
  - `OCI_SUBNET_OCID`
  - `OCI_IMAGE_OCID`
  - `OCI_SSH_PUBLIC_KEY`
  - `OCI_PRIVATE_KEY` or `OCI_PRIVATE_KEY_B64`
- Evidence: provisioning command exits early with missing required OCI env var.
- Note: `KSG_DEV_VM_*` also absent in this session — local dogfood only for L1 ops slice.

## Completed Recently

### T-005: Implement `wait_seconds` long-poll behavior — DONE
- `GET /bus/messages?wait_seconds=N` long-polls (capped by `BUS_WAIT_SECONDS_MAX`, default 30s)
- Tests: timeout empty + early return on new message (`tests/test_bus.py`)
- Smoke: bounded timeout check in `scripts/bus_smoke.sh`
- Docs: README.md + DOCUMENTATION.md

### Ops / observability ladder slice — DONE (this PR)
- Richer `GET /health` gauges + `GET /metrics` JSON counters/gauges/timers
- Structured request logging (`request_id`, method, path, status, duration_ms)
- Ephemeral `POST /agents/register` stub (in-memory; durable registry still TODO)
- Expanded smoke + pytest for metrics, health, orchestration parallel dispatch

## Next Tasks

### T-001: Provision new OCI dev VM (high priority) — blocked by B-001
- Command:
  - `OCI_RUN_DEPLOY_AFTER_CREATE=true python3 scripts/provision-oci-dev-vm.py`
- Expected output:
  - new VM public IP
  - OCI instance ID
  - optional automatic deploy execution
- Acceptance:
  - VM created and reachable over SSH

### T-002: Deploy dev service to the new VM
- Command:
  - `./scripts/deploy-dev-vm.sh`
- Acceptance:
  - `iac-bus-dev.service` active
  - hot reload runner active

### T-003: Verify runtime and logs
- Commands:
  - `curl http://<VM_IP>:8091/health`
  - `curl http://<VM_IP>:8091/metrics`
  - `ssh ... "sudo systemctl status iac-bus-dev.service"`
  - `ssh ... "sudo journalctl -u iac-bus-dev.service -n 100 --no-pager"`
- Acceptance:
  - health endpoint returns `status=ok` with ops fields
  - debug/structured log lines visible

### T-004: Verify GitHub Actions autonomous flows
- Workflows:
  - `.github/workflows/dev-deploy.yml` (triggers on push to `dev`)
  - `.github/workflows/oci-provision-dev-vm.yml`
- Acceptance:
  - test job passes
  - deploy/provision jobs execute successfully with secrets
- Note: create/publish `dev` branch if missing so hot-deploy CI can run.

### T-006: Start SQL-backed message ledger implementation (ACP v2)
- Begin from:
  - `docs/sql/ACP_V2_SCHEMA.sql`
  - `docs/ACP_PROTOCOL_V2.md`
- Scope:
  - persist bus messages and agent identity references
- Acceptance:
  - initial persistence path behind feature flag or adapter layer

### T-007: Durable agent registry (ACP Stage 2–3)
- Replace ephemeral `POST /agents/register` with SQLite-backed registry
- Add heartbeat / last_seen and parent/root relationships
- Acceptance: registry survives restart; tests cover register + lookup

## Operational Notes for New Agent
- First read `prompts/REPO_AGENT_TAKEOVER.prompt.md`.
- Then read this file and execute top-down.
- If blocker persists, do not proceed with fake VM provisioning status.
- Local loop: `BUS_PORT=8101 BUS_API_TOKEN=devtoken` + `scripts/bus_smoke.sh` + `pytest -q`.
