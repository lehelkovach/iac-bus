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
- Local multi-root + KeyChain timing: `docs/LOCAL-AND-OCI-SUITUP.md` §C–D.

## KeyChain (`key-chain-network`) — **later, not now**

**iac-bus does not depend on** [key-chain-network](https://github.com/lehelkovach/key-chain-network)
for M0/M1 (pub/poll, queues, OpenClaw skill, OCI deploy).

Dependency direction (when KeyChain lands):

```text
key-chain-network  →  adapters/iac  →  iac-bus HTTP API
     (grants)            (later)         (transport — this repo)
```

- KeyChain owns **CapabilityGrant / HumanKey / vault / policy** — not message routing.
- IAC owns **channels, queues, claim/ack** — not identity or credential grants.
- Design kickstart lives on KeyChain branch `agent/keychain-2-kickstart` (see that
  repo’s `docs/KEYCHAIN-2-KICKSTART.md`). `main` there is still a placeholder.

**Do not** add a pip/git submodule dependency on KeyChain while shipping bus M0.
Optional sibling checkout for reading design only — see suit-up §D.

## Deploy notes

- Live bus host: `129.153.192.75:8101` (`docs/OCI-LIVE.md`) — **not** port 8091 (OSLO chat).
- Push→deploy: `.github/workflows/dev-deploy.yml` on `master` (needs `KSG_DEV_VM_*` secrets).
- Hotfix default ref: `master`.

## Safety

Never commit tokens. Prefer local Cursor for routine bus work; Cloud only for OCI/SSH.
