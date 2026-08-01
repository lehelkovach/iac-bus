# Clawhub Publication Gate

Publish the IAC Bus OpenClaw skill only after all of these gates pass:

1. Local validation:
   - `pytest -q`
   - `./scripts/bus_smoke.sh`
2. Live OCI smoke against the deployed stack host:
   - deploy with `BUS_PORT=8101`
   - `IAC_BUS_URL=http://<host>:8101 ./scripts/bus_smoke.sh`
3. `osl-oc-agent` dogfood:
   - configure the agent with the live bus URL and token
   - post progress, blocker, and done events from a real agent session
   - claim and ack one queue item from the agent runtime

Do not publish from an untested branch or after local-only validation.

## Skill safety statement

The skill is instruction plus HTTP only:

- no shell tool
- no arbitrary command execution
- no filesystem read/write tools
- no subprocess management
- no SSH or deployment actions

It can still move sensitive data if an agent posts that data to the bus. Agents
must redact secrets and private user data before publishing messages.

## Untrusted registry rules

Treat any third-party Clawhub or skill registry as untrusted until verified:

- Pin the exact skill version or commit digest.
- Review manifest `runtime`, `entrypoint`, and every declared tool before import.
- Reject manifests that add shell, filesystem, network pivot, or credential
  exfiltration capabilities outside the documented HTTP API.
- Require explicit operator approval before enabling a new registry source.
- Prefer private/internal registries for production agents.

## Release checklist

- `skills/openclaw/iac_bus.yaml` matches the checked-in Python adapter.
- `docs/OPENCLAW_SKILL.md` reflects the live endpoint and token configuration.
- The live OCI endpoint runs on `BUS_PORT=8101` unless a stack owner documents a
  different non-conflicting port.
- Dogfood notes include the test session ID and queue worker ID.

## Status (2026-08-01 — engaged)

| Gate | Status |
|------|--------|
| Local pytest + bus_smoke | **Pass** (32 pytest; unique-queue smoke green) |
| Live OCI deploy `:8101` | **Pass** — `iac-bus-6c58` / `129.153.192.75` (public + bearer) |
| osl-oc-agent dogfood | **Pass** — full `iac_bus_demo` claim/ack + prod chat auto-announce to `ops` |
| ClawHub publish | **Ready to publish skill package** (HTTP-only). Still optional for Stage-1 product. |

**Publish now if** you want the OpenClaw skill discoverable for multi-agent ops.  
**Skip if** you are only shipping single-agent autofill — the bus is engaged for your stack either way via env + auto-announce.
