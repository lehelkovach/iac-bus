# Agent Task Queue (Takeover Canonical)

Status owner: any active takeover agent  
Last updated: 2026-05-27 UTC  
Purpose: single file a new agent reads to continue work safely.

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
