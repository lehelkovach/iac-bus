# Agent Task Queue (Takeover Canonical)

Status owner: any active takeover agent  
Last updated: 2026-08-26 UTC  
Purpose: single file a new agent reads to continue work safely.

## Progress pointer
See `docs/PROGRESS.md` for ladder status, dogfood evidence, and live OCI edge
(`iac-bus.knowshowgo.com` → `129.153.192.75:8101`).

## Current Blockers

### B-001: Full OCI provision secret set incomplete in cloud agent
- Severity: blocker for *new* VM provision
- Missing typical OCI_* provision secrets (tenancy/user/fingerprint/…).
- `OCI_COMPARTMENT_ID` + API signing key may be present; VM SSH deploy keys
  (`IAC_BUS_PROD_KEY` / `KSG_DEV_VM_KEY`) are still required for CI deploy.

### B-002: DNS for public edge
- `iac-bus.knowshowgo.com` NXDOMAIN as of 2026-08-26 — create A → `129.153.192.75`.
- Health today: `http://129.153.192.75:8101/health`.

### B-003: Deploy SSH from this environment
- Instance `iac-bus-6c58` is RUNNING; SSH with API PEM fails (expected).
- Inject `IAC_BUS_PROD_KEY` (or `KSG_DEV_VM_KEY`) to enable `scripts/deploy-prod-vm.sh`.


## Next Tasks (ready when blocker clears)

### T-001: Provision new OCI dev VM (high priority)
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
  - `ssh ... "sudo systemctl status iac-bus-dev.service"`
  - `ssh ... "sudo journalctl -u iac-bus-dev.service -n 100 --no-pager"`
- Acceptance:
  - health endpoint returns `status=ok`
  - debug log lines visible

### T-004: Verify GitHub Actions autonomous flows
- Workflows:
  - `.github/workflows/dev-deploy.yml`
  - `.github/workflows/oci-provision-dev-vm.yml`
- Acceptance:
  - test job passes
  - deploy/provision jobs execute successfully with secrets

### T-005: Implement `wait_seconds` long-poll behavior
- Target:
  - `GET /bus/messages` supports long-poll timeout behavior
- Tests:
  - add integration tests for timeout and early-return on new message
- Acceptance:
  - tests pass and behavior documented

### T-006: Start SQL-backed message ledger implementation (ACP v2)
- Begin from:
  - `docs/sql/ACP_V2_SCHEMA.sql`
  - `docs/ACP_PROTOCOL_V2.md`
- Scope:
  - persist bus messages and agent identity references
- Acceptance:
  - initial persistence path behind feature flag or adapter layer

## Operational Notes for New Agent
- First read `prompts/REPO_AGENT_TAKEOVER.prompt.md`.
- Then read this file and execute top-down.
- If blocker persists, do not proceed with fake VM provisioning status.
