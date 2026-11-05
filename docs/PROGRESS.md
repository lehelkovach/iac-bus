# IAC-Bus progress tracker

_Last updated: 2026-11-03. Dated facts are readings, not standing promises._

## Ladder status

The plan of record for identity and coordination is
`docs/IDENTITY-AND-AUTHORITY.md` (ID0–ID6). Its rung names map onto this ladder.

| Rung | Goal | Status |
| --- | --- | --- |
| **L0** | HTTP bus, queues, orchestration jobs, local smoke/pytest | **Done** (master) |
| **L1** | `wait_seconds`, richer `/health` + `/metrics`, structured logs, expanded smoke | **Done** on `dev` (PR #17) |
| **L2** | Agent registry + per-run identity (ID1–ID2) | Stub `POST /agents/register` on `dev`; run tokens planned as coordination Phase 2b |
| **L3** | Tasks + dependency invalidation + provenance | Not started |
| **L4** | Repo/path locks + fencing (ID3) | **Phase 1 in review**: in-memory locks, `/bus/locks/*`, fencing `validate` |
| **L5** | Durable ledger (SQLite→Postgres), retries/DLQ, prod maturity | Partial: opt-in SQLite store on `dev` (PR #20); locks and jobs still in memory |

KeyChain is **out of ladder scope** (not a bus dependency).

## Coordination plan (2026-11-03)

Goal: iac-bus becomes the coordination layer for several coding agents on one
repo (lock, claim, ask permission, over MCP) and later lets A2A agents join
supervised work. The existing HTTP API keeps working throughout. Each phase is
its own branch and PR to `dev`, stacked on the previous phase.

| Phase | Scope | Branch | Status |
| --- | --- | --- | --- |
| 1 | L4 path locks with fencing (in memory) | `claude/iac-bus-p1-locks-vhy9iw` | In review (PR #21) |
| 2 | MCP adapter (`mcp_server.py`, stdio) + two-agent demo | `claude/iac-bus-p2-mcp-vhy9iw` | In review |
| 2b | Per-run tokens; the bus derives the caller from its token | — | Planned |
| 3 | A2A mapping (design only), then pause for review | — | Planned |
| 4 | A2A interop (Agent Card, JSON-RPC, card registration) | — | Waits for approval of Phase 3 |

### Phase 1 facts

- Endpoints: `POST /bus/locks/acquire|renew|release|validate`, `GET /bus/locks`.
- Overlap is by path segment and scoped by `repo`; `shared` vs `exclusive`;
  locks by the same agent never conflict.
- Fencing tokens are strictly increasing and seeded from the clock in ms.
  `validate` rejects a token whose lock was released, expired or taken over.
- Lock ids are only returned to the acquirer; events on channel `locks`.
- Identity is still the caller-supplied `agent` name. Every handler now reads
  it through one helper (`_actor_from_request`) so Phase 2b swaps it in one
  place.
- Gates: `pytest -q` green, `scripts/bus_smoke.sh` 15/15 locally.
- Known gap: `resource_locks.resource_key UNIQUE` in
  `docs/sql/ACP_V2_SCHEMA.sql` cannot hold several shared holders (TODO in
  the schema and `locks.py`).

### Phase 2 facts

- `mcp_server.py`: stdio MCP server (official SDK, `mcp>=2.3,<3`), a thin
  client over the HTTP API. Config: `BUS_URL`, `BUS_API_TOKEN`,
  `BUS_AGENT_ID`, plus `BUS_REPO`, `BUS_REPO_ROOT`, `BUS_AGENT_SUFFIX`.
- 13 tools: the 11 planned plus `check_lock` (fencing check before commit)
  and `check_decision` (poll an earlier `request_decision`). Descriptions and
  server instructions tell agents to lock before editing and release after.
- Each process appends a random suffix to `BUS_AGENT_ID`, so two sessions
  sharing a committed `.mcp.json` cannot share an identity.
- The lock scope defaults to the origin remote's repo name, so worktrees and
  clones of one repo see each other's locks.
- `scripts/demo_two_agents.sh` drives two real adapter processes over MCP
  stdio through a lock hand-over. `docs/MCP_SETUP.md` covers Claude Code.
- `requirements-mcp.txt` is installed by CI only; the dev VM's
  `requirements-dev.txt` is unchanged (the SDK needs Python 3.10+).
- Gates: `pytest -q` green with the SDK and without it (2 MCP-surface tests
  skip), `scripts/bus_smoke.sh` 15/15, demo exits 0.

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
4. Review and merge coordination Phase 1 (locks) into `dev`, then Phase 2 (MCP).
5. Dogfood two Claude Code sessions on one repo through the MCP adapter.
