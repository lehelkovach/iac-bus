# IAC-Bus progress tracker (`dev` tip)

_Last updated: 2026-08-28._ This tip does not yet carry `master`’s `OCI-LIVE.md` /
`DOGFOOD-LITMUS.md`. Live-edge facts: see those files on **`master`**, or
[`docs/TDD-LADDER.md`](./TDD-LADDER.md).

## Ladder status

| Rung | Goal | Status on **this `dev` tip** |
| --- | --- | --- |
| **L0** | HTTP bus, queues, local smoke/pytest | Done |
| **L1** | `wait_seconds`, richer `/health` + `/metrics` | **Merged (#17)** — not promoted to `master` |
| **L2+** | registry, tasks, locks, ledger | Not started — after L1 edge dogfood, not for Influence A–F |

**Influence A–F do not use this bus.** Process: [`TDD-LADDER.md`](./TDD-LADDER.md)
and KSG `docs/TDD-LADDER.md`.

## Next

1. Do not start L2 to unblock KSG/OSL Influence.
2. Promote L1 → `master` only after pytest + `bus_smoke.sh` and a token smoke
   against the edge (blocked on DNS/SSH secret **names** `IAC_BUS_PROD_KEY` /
   `KSG_DEV_VM_KEY`).
