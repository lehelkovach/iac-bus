# AGENTS.md

Canonical instructions for coding agents working in `iac-bus`.

## Continuity (v2)

- **Policy:** this file. **Ops loop:** [`.AGENT/RUNBOOK.md`](.AGENT/RUNBOOK.md).
- **Cross-repo env:** `osl-oc-agent/.AGENT/handoffs/CURSOR-ENV-HANDOFF.md` (secret *names* only).
- **Handoffs:** copy [`.AGENT/handoffs/HANDOFF-TEMPLATE.md`](.AGENT/handoffs/HANDOFF-TEMPLATE.md) to a uniquely named task file only when state must cross sessions before a PR exists.
- **Do not** append `.AGENT/agent-action-log.md` or use run-once queues — archived under `.AGENT/archive/v1/`. Use issues, commits, and PRs.
- Template source: sibling `agent-repo-boilerplate` (`main`). Re-sync: `python3 ../agent-repo-boilerplate/scripts/apply_template.py .` (dry-run) then `--apply`.
- Gitflow: integration tip is **`master`**. Branch from **`master`**, PR into **`master`**.

## Repo role

Inter-Agent Communication Bus (IAC Bus): a lightweight Flask HTTP message bus for
coordinating multiple agents (pub/sub channels + queue leasing). Also hosts OCI
dev-VM provisioning and Cursor-agent spawn/terminate scripts under `scripts/`.

**Start:** [`README.md`](README.md) → [`docs/WHEN-NEEDED.md`](docs/WHEN-NEEDED.md).
OpenClaw skill: [`docs/OPENCLAW_SKILL.md`](docs/OPENCLAW_SKILL.md).

## Commands

```bash
python3 -m pip install -r requirements.txt -r requirements-dev.txt
pytest
# or: pytest tests/
```

## Safety

- Never commit secret values (`IAC_BUS_TOKEN`, OCI creds, Cursor tokens).
- Prefer editing existing files; keep changes reviewable.
- State exactly what was and was not verified.
