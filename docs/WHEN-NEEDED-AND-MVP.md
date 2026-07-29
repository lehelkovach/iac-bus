# IAC Bus — When You Need It + Parallel MVP

**Date:** 2026-07-25  
**Tone:** no sugar.

---

## Verdict: when is this code needed?

| Your goal | Need iac-bus? |
|---|---|
| Sell Stage-1 autofill PA (one agent, KSG memory, WebIO) | **No.** Single agent + KSG is enough. |
| Dogfood Slack/chat on one session | **No.** |
| Topics/tags/Token Viewer | **No.** |
| Spawn Cursor Cloud Agents / coordinate parallel cloud workers | **Yes — light.** Spawn/terminate scripts + bus announce already exist (PR #4 auth fix). |
| Multi-agent Stage-2 (mobile farm + web + phone sub-agents) | **Yes.** Bus is the generic coordination layer; do not put KSG/OpenClaw logic here. |
| “ACP v0.1 full roadmap” (SQLite ledger, locks, Slack bridge, provenance…) | **Later.** Useful, not on the money path until multi-agent is real. |

**Rule:** IAC-Bus is **ops/coordination infrastructure**, not the product. Product = KSG memory + autofill agent. Turn the bus up when you have **more than one worker** that must not step on each other.

Layer split (locked):
- **IAC Bus** — generic pub/sub + work lease + spawn orchestration  
- **KSG** — semantic memory  
- **OpenClaw / osl-oc-agent** — reason-act + tools  
- **redroid / PhoneIO** — hands (Stage 2)

---

## What already works

- Flask bus: post/poll messages, queue claim/ack/nack, optional bearer auth (`server.py`)
- Cursor spawn/terminate scripts (Bearer auth fix on `cursor/spawn-cursor-agents-auth-6c58` — **merge that**)
- Unit tests under `tests/`
- Big plans in `docs/ROADMAP.md` / `FULL_DEV_PLAN.md` — aspirational; do not implement the whole roadmap in one pass

---

## Parallel MVP (fast agent — stop when done)

Cheap, high-leverage only. **No** Postgres, **no** OPA, **no** Slack flood bridge, **no** embedding work.

### M1 — Land spawn path
1. Ensure spawn/terminate Bearer auth + no invalid `name` field is on the working branch (cherry-pick/merge from `cursor/spawn-cursor-agents-auth-6c58` if not on master).
2. Document one command path in README: start bus → spawn 1 agent → see `ops` announce → terminate.
3. Dry-run / unit tests for spawn request body shape (mock HTTP) so CI doesn’t need `CURSOR_API_KEY`.

### M2 — Bus smoke + CI gate
1. `pytest` green locally.
2. Minimal GitHub Actions workflow: install reqs → pytest (no OCI secrets required).
3. `scripts/bus_smoke.sh` (or `.py`): health → post → poll → claim/ack round-trip against local server.

### M3 — Tiny coordination contract (optional if M1–M2 finish early)
1. Document canonical message types used by spawn scripts (`spawned:…`, `terminated:…`) on `ops`.
2. If trivial: persist messages to SQLite **only if** in-memory already has a clear extension point — otherwise skip and leave a TODO pointing at ROADMAP v0.1.

### Explicit non-goals
- OCI VM provisioning (needs secrets; not this agent’s job unless secrets present)
- Embedding KSG/OpenClaw business logic into the bus
- Full ACP protocol v2
- Draining Cursor Plus on giant refactors

---

## Definition of done

- [x] M1 scripts correct + mocked unit tests  
- [x] M2 pytest CI + local smoke script  
- [x] Draft PR(s) to `master` (this repo has no `dev` yet — establish `dev` off `master` if easy, else PR to `master`)  
- [x] This file linked from README “When to use”  
- [x] v0.1 coordination MVP: SQLite messages/leases, agent registry/heartbeat, repo/path locks, `wait_seconds`  

---

## Cost note (Cursor Plus)

Use a **fast** model for this track. Cap scope to M1–M2. One PR. Stop. Do not “assimilate FULL_DEV_PLAN.”
