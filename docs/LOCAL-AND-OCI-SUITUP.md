# Local and OCI suit-up

How to get iac-bus running. **Default path is local.** OCI/Cloud only when the human asks.

KeyChain (`key-chain-network`) is **not** required for this repo.

## Local suit-up (M0)

### Prerequisites

- Python 3.10+ (3.12+ preferred)
- `curl` (for smoke)
- Bash (Git Bash on Windows is fine; WSL optional)

### Setup

```bash
git checkout master
git pull

python3 -m venv venv
./venv/bin/pip install --upgrade pip
./venv/bin/pip install -r requirements.txt -r requirements-dev.txt
```

Windows PowerShell:

```powershell
python -m venv venv
.\venv\Scripts\python.exe -m pip install --upgrade pip
.\venv\Scripts\pip.exe install -r requirements.txt -r requirements-dev.txt
```

### Run the bus

Recommended local ports/token for agent work (avoids clashing with default `8091`):

```bash
BUS_HOST=127.0.0.1 BUS_PORT=8101 BUS_API_TOKEN=devtoken ./venv/bin/python server.py
```

Health check:

```bash
curl -fsS http://127.0.0.1:8101/health
# {"status":"ok", ...}
```

### Smoke + tests

With the server running:

```bash
BUS_PORT=8101 BUS_API_TOKEN=devtoken ./scripts/bus_smoke.sh
./venv/bin/pytest -q
```

`conftest.py` puts the repo root on `sys.path`; no `PYTHONPATH` export is required.

Smoke covers: `/health`, unauthenticated `401`, post, channel poll, `since_id`, empty queue claim `204`.

### Useful env vars

| Variable | Local default used here | Notes |
| --- | --- | --- |
| `BUS_HOST` | `127.0.0.1` or `0.0.0.0` | Bind address |
| `BUS_PORT` | `8101` (local M0) / `8091` (repo default) | Listen port |
| `BUS_API_TOKEN` | `devtoken` | Bearer token; empty disables auth |
| `BUS_LOG_LEVEL` | `INFO` | Use `DEBUG` for verbose local debug |

## Public edge

Intended hostname: **`iac-bus.knowshowgo.com`** (A → OCI `iac-bus-6c58` public IP).
See `docs/OCI-LIVE.md`. Prefer local loop until DNS/TLS are verified.

## OCI / Cloud (when Captain asks — approved for edge work)

Do **not** provision or deploy unless the human explicitly requests it.

### When you need deploy docs

1. Read `OCI_DEPLOYMENT.md` for VM bootstrap + systemd deploy.
2. If present, read `docs/OCI-LIVE.md` for live cutover specifics.
3. Dev VM automation lives in:
   - `scripts/deploy-dev-vm.sh`
   - `scripts/provision-oci-dev-vm.py`
   - `.github/workflows/dev-deploy.yml`
   - `.github/workflows/oci-provision-dev-vm.yml`

### Secrets (do not invent values)

Dev VM deploy may need: `KSG_DEV_VM_HOST`, `KSG_DEV_VM_USER`, `KSG_DEV_VM_KEY`, optional `KSG_DEV_VM_PORT`, `KSG_DEV_VM_APP_DIR`, `BUS_API_TOKEN`.

OCI provision may need: `OCI_TENANCY_OCID`, `OCI_USER_OCID`, `OCI_FINGERPRINT`, `OCI_REGION`, `OCI_COMPARTMENT_OCID`, `OCI_SUBNET_OCID`, `OCI_IMAGE_OCID`, `OCI_SSH_PUBLIC_KEY`, and `OCI_PRIVATE_KEY` or `OCI_PRIVATE_KEY_B64`.

If secrets are missing, stop and report the blocker (see `docs/AGENT_TASKS.md`).

### Verify on a VM (after a real deploy)

```bash
curl http://<VM_IP>:8091/health
ssh ... "sudo systemctl status iac-bus-dev.service"
ssh ... "sudo journalctl -u iac-bus-dev.service -n 100 --no-pager"
```

## Out of scope unless asked

- Changing GitHub/deploy secrets
- KeyChain integration
- Live production cutover
- Force-push / history rewrite
