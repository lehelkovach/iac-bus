# IAC-Bus progress tracker

_Last updated: 2026-11-03. Dated facts are readings, not standing promises._

## Ladder status

The plan of record for identity and coordination is
`docs/IDENTITY-AND-AUTHORITY.md` (ID0–ID6). Its rung names map onto this ladder.

| Rung | Goal | Status |
| --- | --- | --- |
| **L0** | HTTP bus, queues, orchestration jobs, local smoke/pytest | **Done** (master) |
| **L1** | `wait_seconds`, richer `/health` + `/metrics`, structured logs, expanded smoke | **Done** on `dev` (PR #17) |
| **L2** | Agent registry + per-run identity (ID1–ID2) | **Phase 2b in review**: run tokens, bus-derived identity; registry still a stub (no principals, no dedupe) |
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
| 2 | MCP adapter (`mcp_server.py`, stdio) + two-agent demo | `claude/iac-bus-p2-mcp-vhy9iw` | In review (PR #22) |
| 2b | Per-run tokens; the bus derives the caller from its token | `claude/iac-bus-p2b-run-tokens-vhy9iw` | In review (PR #23) |
| 3 | A2A mapping (design only), then pause for review | `claude/iac-bus-p3-a2a-mapping-vhy9iw` | In review (PR #24); approved 2026-11-03 |
| 4 | A2A interop (Agent Card, JSON-RPC, card registration, grants, outbound bridge) | `claude/iac-bus-p4-a2a-vhy9iw` | In review |

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

### Phase 2b facts

- `POST /agents/register` now also starts a run and returns `run_token`
  (`iacrun_...`, stored as a SHA-256 hash) and `actor` (`<handle>#<run>`).
- A request with a run token authenticates as that run; `_actor_from_request`
  returns the run's actor and ignores `sender` / `worker` / `agent` in the
  body. The shared token keeps working with claimed names.
- Runs slide-expire after `run_ttl_seconds` without use; `POST
  /agents/heartbeat` extends them; `POST /agents/runs/end` revokes the token
  and releases the run's locks. `GET /agents/whoami`, `GET /agents/runs`.
- `BUS_REQUIRE_RUN_TOKEN=1` makes `/bus/*` refuse the shared token.
- The MCP adapter registers a run at startup, heartbeats, re-registers after
  a bus restart, and ends its run on exit. `BUS_RUN_TOKEN` accepts a
  launcher-minted token; `BUS_RUN_REGISTER=0` keeps the Phase 2 behaviour.
- Runs are in memory in both store backends, like locks.
- Not done (Track B): principals, grants, runs minting child runs, signatures.
- Gates: `pytest -q` green, `scripts/bus_smoke.sh` 18/18, demo exits 0 and
  shows a refused spoof.

### Phase 3 facts

- `docs/A2A_MAPPING.md` maps A2A 1.0 (released 2026-03-12; wire version
  "1.0") onto the bus: Agent Card, JSON-RPC methods, task states both ways,
  auth, and what A2A lacks (locks, leases, hierarchy, authority, registry).
- The card path is `/.well-known/agent-card.json`, not `agent.json`.
- Phase 4 needs decisions A–G in §7 of that doc before any A2A code.

### OCI deploy and Claude trials (2026-11-03)

- `iac-bus-coord-dev` (`129.146.71.34:8101`) runs the stack tip, deployed
  through the OCI API with `scripts/oci_launch_bus_vm.py`; `/health` reports
  `0.2.0-dev` at `aeb329a` (Phase 4 included) and the A2A card is public. The production VM `iac-bus-6c58` is unchanged.
  Details and the HTTPS blocker: `docs/OCI-LIVE.md`.
- `scripts/claude_agents_trial.py` runs real `claude -p` sessions through the
  MCP adapter. Prompts never mention the bus. First run (local bus, Sonnet):

| Scenario | Result | What happened |
| --- | --- | --- |
| locks | 6/6 | Both agents locked `src/app.py` before editing; B got it, A was refused, waited, re-read, edited; both functions survived |
| decision | 0/3 | Agent searched, judged the file unused and deleted it without asking |
| queue | 4/5 | Each agent claimed a different task, did it and acked it; the failed check was a harness bug |

- Fix: the adapter now states an approval policy in its MCP instructions
  (`BUS_APPROVAL_FOR`, default: deleting or renaming files, merging,
  deploying, changing a public API or schema), which applies even when the
  task asks for the step.
- Re-run on the fixed adapter: locks 6/6, decision 3/3 (the agent asked,
  the scripted human said no, the file stayed), queue 5/5. Full transcript:
  `docs/CLAUDE_TRIAL.md`.
- Not yet run against the OCI bus: it needs HTTPS first (owner decision on
  opening 80/443 for that VM).

### Phase 4 facts

- Inbound: public Agent Card, `POST /a2a` (SendMessage, GetTask, ListTasks,
  CancelTask), `POST /agents/a2a/register`, `GET /agents/a2a`; grants on runs
  enforced on `/a2a` and `/bus/*`. Outbound: `scripts/a2a_bridge.py`.
- Wire JSON is checked against the official `a2a-sdk` protobuf types in tests
  (`requirements-a2a-test.txt`, CI only).
- Gates: `pytest -q` green, `scripts/bus_smoke.sh` 20/20,
  `scripts/a2a_curl_demo.sh` runs end to end (output in `docs/A2A_DEMO.md`).
- A2A tasks are in memory, like locks and runs.

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
4. Review and merge coordination Phases 1, 2 and 2b into `dev` in order (#21, #22, then 2b).
5. Dogfood two Claude Code sessions on one repo through the MCP adapter.
