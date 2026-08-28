# Continuity digest — iac-bus (`dev` / integration)

**Updated:** 2026-08-28

## Now

- This tip is **`dev`** (integration / hot-reload). Release is **`master`**. They are **not** the same branch.
- **TDD process:** [`docs/TDD-LADDER.md`](../docs/TDD-LADDER.md) → canonical [`knowshowgo/docs/TDD-LADDER.md`](https://github.com/lehelkovach/knowshowgo/blob/dev/docs/TDD-LADDER.md).
- **L1** (`wait_seconds` / metrics) is **merged on this tip** (#17). It is **not** on `master`.
- Influence A–F do **not** depend on this bus. Do not start L2 to “unblock” Influence.
- Ops docs (`PROGRESS.md`, `OCI-LIVE.md`, `DOGFOOD-LITMUS.md`) currently live on **`master`** — this tip diverged. Do not copy them blindly; link until a dedicated sync PR.

## Holds

- DNS / SSH deploy secrets (names: `IAC_BUS_PROD_KEY`, `KSG_DEV_VM_KEY`) — edge dogfood blocked.
- KeyChain is not a dependency.

## Next

Promote L1 to `master` only after local pytest + smoke, then token smoke against the live edge. Meanwhile the KSG stack’s next merge is composition S1, not this repo.
