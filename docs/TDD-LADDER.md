# TDD ladder — iac-bus (`master` / release)

**You are on `master` (release).** Integration is **`dev`**. They are different branches.

Canonical process:
[`knowshowgo/docs/TDD-LADDER.md` on `dev`](https://github.com/lehelkovach/knowshowgo/blob/dev/docs/TDD-LADDER.md).

This repo vs Influence: **A–F do not use IAC Bus.** L1 is merged on `dev` (#17),
not on this tip. Promote L1 here only after pytest + smoke + token edge check.

Do not start L2, KeyChain, or Influence rungs from `master`. Branch from `dev`.
