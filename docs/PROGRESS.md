# IAC-Bus progress tracker

_Last updated: 2026-08-26. Dated facts are readings, not standing promises._

## Ladder status

| Rung | Goal | Status |
| --- | --- | --- |
| **L0** | HTTP bus, queues, orchestration jobs, local smoke/pytest | **Done** (master) |
| **L1** | `wait_seconds`, richer `/health` + `/metrics`, structured logs, expanded smoke | **In flight** — branch `cursor/ladder-ops-metrics-e357` |
| **L2** | Agent UUID registry + heartbeat | Not started |
| **L3** | Tasks + dependency invalidation + provenance | Not started |
| **L4** | Repo/path locks + fencing | Not started |
| **L5** | Durable ledger (SQLite→Postgres), retries/DLQ, prod maturity | Not started |

KeyChain is **out of ladder scope** (not a bus dependency).

## Active agent tracks (2026-08-26)

| Track | Branch / agent | Focus |
| --- | --- | --- |
| Ops / metrics / long-poll | `cursor/ladder-ops-metrics-e357` | L1 observability + tests |
| AGENTS / version / CI / OCI docs | `cursor/agents-version-ci-oci-e357` | VERSION, prod CI, dogfood litmus, OCI-LIVE |

## Dogfood evidence (local + nested)

| Job | Result |
| --- | --- |
| `dogfood-nested-20260825` | All steps completed (bootstrap + 2 workers). Nested Cursor Task spawn unavailable inside orchestrator subagent; workers ran via bus claim/ack. |
| `dogfood-nested-spawn-proof` | Parent Task spawn → worker claim/complete **passed**. |

## Live OCI edge (measured 2026-08-26)

| Item | Value |
| --- | --- |
| VM display name | `iac-bus-6c58` |
| Public IP | `129.153.192.75` |
| Private IP | `10.0.1.10` |
| Health (no auth) | `GET http://129.153.192.75:8101/health` → `{"status":"ok",...}` |
| API auth | Non-`/health` routes return **401** without bearer token (token present on host) |
| Intended DNS | **`iac-bus.knowshowgo.com`** → `129.153.192.75` (**NXDOMAIN** as of 2026-08-26 — create A record) |
| SSH from this cloud agent | **Blocked** — `OCI_SSH_PRIVATE_KEY` is API signing material, not VM login; `KSG_DEV_VM_KEY` / `IAC_BUS_PROD_KEY` missing |

## Next concrete actions

1. DNS: A record `iac-bus.knowshowgo.com` → `129.153.192.75` (optional CNAME/www later).
2. TLS: Caddy/nginx on VM for `:443` → bus `:8101` (or migrate prod listen to `:8091` behind proxy).
3. Inject deploy secrets (`IAC_BUS_PROD_*` or `KSG_DEV_VM_*`) for CI hot/prod deploys.
4. Land L1 PR → `dev` → dogfood litmus against edge with token.
5. L2 agent registry once metrics/smoke are green on `dev`.
