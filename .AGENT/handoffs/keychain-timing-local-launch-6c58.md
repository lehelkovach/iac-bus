# Handoff: KeyChain timing + local iac-bus launch

## Verdict
**iac-bus does not depend on key-chain-network now.** KeyChain will adapt *to*
IAC later (`adapters/iac` → this HTTP API). Open only `iac-bus` for local M0.

## Agent ingestion (local Cursor project)
1. Root `AGENTS.md`
2. `docs/LOCAL-AND-OCI-SUITUP.md` (§A launch, §D KeyChain)
3. Smoke: venv → `BUS_PORT=8101` → `./scripts/bus_smoke.sh` → `pytest -q`

## Optional siblings
OSLO / KSG / client for later dogfood. KeyChain clone = design-only until adapter PR
in that repo (`agent/keychain-2-kickstart`).

## Live bus
`129.153.192.75:8101` — not OSLO `:8091`.
