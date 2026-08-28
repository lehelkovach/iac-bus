# Continuity digest — iac-bus (`master` / release)

**Updated:** 2026-08-28 · This tip is **release**.

## Now

- **`master` = release.** Integration / hot-reload is **`dev`**. `dev` is not `master`.
- **TDD process:** [`docs/TDD-LADDER.md`](../docs/TDD-LADDER.md) → KSG ladder on `dev`.
- L0 is on this tip. **L1 is on `dev` (#17), not here.** `docs/PROGRESS.md` previously said L1 in flight — that referred to a branch that has since merged to `dev`.
- Influence A–F do **not** depend on this bus.

## Holds

- DNS `iac-bus.knowshowgo.com` NXDOMAIN (last measured). Health: VM IP `:8101`.
- SSH deploy secret names `IAC_BUS_PROD_KEY` / `KSG_DEV_VM_KEY` missing.
- KeyChain is not a dependency.

## Anti-drift

Keep this file short. Dated edge facts stay in `docs/PROGRESS.md` / `docs/OCI-LIVE.md`.
