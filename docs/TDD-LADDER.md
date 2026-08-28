# TDD ladder — iac-bus

**This repo is out of Influence rungs A–F.** Canonical process:
[`knowshowgo/docs/TDD-LADDER.md`](https://github.com/lehelkovach/knowshowgo/blob/dev/docs/TDD-LADDER.md).

**Integration tip is `dev`. Release tip is `master`. `dev` is not `master`.**

## Slice law

```text
failing test → implement → local green → PR into `dev`
→ merged-`dev` CI (`dev-deploy.yml` pytest job, or local pytest + bus_smoke)
→ prod decision (tag / merge to `master` only when the bus slice is prod-facing)
→ only then start the next bus rung
```

Local gate (this repo has no substitute for pytest):

```bash
BUS_PORT=8101 BUS_API_TOKEN=devtoken bash ./scripts/bus_smoke.sh
./venv/bin/pytest -q
```

## This repo vs Influence

| Influence rung | IAC Bus role |
|---|---|
| A–F | **None.** OSL ICBus is in-process. Do not wire this service as a runtime. |
| After F | Optional **cross-process transport provider** if two processes must share a graph |
| Closed OSL #109 `IacBusSkill` | Agent **tool**, never the transport. Do not merge that as ICBus. |

## Bus ladder (own track — do not block KSG S1)

| Rung | Goal | Close-out | Merged-`dev` | Prod (`master`)? |
|---|---|---|---|---|
| L0 | HTTP bus, queues, smoke | `bus_smoke.sh` + pytest | Done (also on `master`) | Yes — already |
| L1 | `wait_seconds`, richer health/metrics | pytest + expanded smoke | **Merged here (#17)** | **Not yet** — promote after token smoke against the edge |
| L2+ | registry, tasks, locks, ledger | named in `docs/ACP_DEV_PLAN.md` | After L1 CI + edge dogfood | After `dev` |

## Do not start from here

KSG composition S1, OSL runtime S2, Influence A–F, ComputeNet, KeyChain.

## Blockers (names only)

`IAC_BUS_PROD_KEY` / `KSG_DEV_VM_KEY` missing for SSH deploy. DNS
`iac-bus.knowshowgo.com` was NXDOMAIN when last measured. Health today is the
VM IP `:8101`. Do not invent DNS/SSH success.
