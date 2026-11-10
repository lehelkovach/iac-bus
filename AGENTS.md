# Agent instructions (iac-bus)

Tip branch: `master` (release). Integration tip: `dev` (hot-reload).  
Public edge (intended): **`https://iac-bus.knowshowgo.com`** → OCI VM `iac-bus-6c58`
(`129.153.192.75`, bus currently on **:8101**). DNS A record may still need creating.

## Ingest order

1. This file (`AGENTS.md`)
2. `docs/PROGRESS.md` (ladder status + active agent tracks)
3. `docs/LOCAL-AND-OCI-SUITUP.md`
4. `docs/DOGFOOD-LITMUS.md` — when coordinating parallel agents via the bus
5. `docs/OCI-LIVE.md` / `OCI_DEPLOYMENT.md` — deploy / DNS / prod cutover

## Hard constraints

- **KeyChain (`key-chain-network`) is not a dependency.** Do not add it, wire it, or assume it.
- Do not invent OCI/VM/DNS success when secrets, SSH, or DNS are missing.
- Never print secret values; record required secret **names** only.
- Keep diffs scoped to the requested task.

## Default local loop (M0)

```bash
python3 -m venv venv
./venv/bin/pip install -r requirements.txt -r requirements-dev.txt

BUS_PORT=8101 BUS_API_TOKEN=devtoken ./venv/bin/python server.py
# other terminal:
BUS_PORT=8101 BUS_API_TOKEN=devtoken bash ./scripts/bus_smoke.sh
./venv/bin/pytest -q
```

Acceptance: smoke all green; `pytest -q` all green.

## Dogfood + litmus (parallel agents)

When the bus is stable locally or on the OCI edge:

1. Run non-blocking worker slices via bus orchestration (or Cursor Task + bus claim/ack).
2. Periodically run **`scripts/dogfood_litmus.sh`** — parallel workers then a **barrier sync**
   step (litmus: coordination must reconverge, not just fan-out).
3. Prefer channel `dogfood` / queue `orchestration`. See `docs/DOGFOOD-LITMUS.md`.

Do **not** block the human on long dogfood loops; background them and report evidence.

## Branch / version / deploy

| Track | Branch | Version file | CI |
| --- | --- | --- | --- |
| Hot-reload / integration | `dev` | `VERSION` (`*-dev`) | `.github/workflows/dev-deploy.yml` |
| Release / prod | `master` (tag `vX.Y.Z`) | `VERSION` (semver) | `.github/workflows/prod-deploy.yml` |

- Feature PRs: `cursor/<name>-e357` → prefer **`dev`**, then promote to **`master`**.
- Version source: `VERSION` + `version.py` (surfaced on `/health`).
- OCI public hostname: `iac-bus.knowshowgo.com` (A → VM public IP; TLS via Caddy preferred).

## Secrets (names only)

| Purpose | Names |
| --- | --- |
| Dev hot-deploy | `KSG_DEV_VM_HOST`, `KSG_DEV_VM_USER`, `KSG_DEV_VM_KEY`, optional port/app dir, `BUS_API_TOKEN` |
| Prod deploy | `IAC_BUS_PROD_HOST`, `IAC_BUS_PROD_USER`, `IAC_BUS_PROD_KEY`, `BUS_API_TOKEN` |
| OCI provision | `OCI_TENANCY_OCID`, `OCI_USER_OCID`, `OCI_FINGERPRINT`, `OCI_REGION`, `OCI_COMPARTMENT_OCID`, `OCI_SUBNET_OCID`, `OCI_IMAGE_OCID`, `OCI_SSH_PUBLIC_KEY`, `OCI_PRIVATE_KEY` or `_B64` |

## Where to look

| Need | Location |
| --- | --- |
| Progress / ladder | `docs/PROGRESS.md` |
| HTTP API / env | `README.md`, `DOCUMENTATION.md` |
| Live OCI + DNS | `docs/OCI-LIVE.md` |
| Tests / gates | `docs/TESTING_STRATEGY.md` |
| Task queue | `docs/AGENT_TASKS.md` |
| Protocol | `docs/ACP_PROTOCOL_V2.md`, `docs/ACP_DEV_PLAN.md` |

## Working rules

- Prove changes with local smoke + pytest before claiming done.
- Update `docs/PROGRESS.md` when a ladder rung or deploy fact changes.
- If blocked on missing secrets/DNS/SSH, report the blocker; do not fake progress.
