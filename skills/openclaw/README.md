# OpenClaw IAC Bus Skill

This directory contains reusable skill artifacts for exposing IAC Bus to
OpenClaw-style agents.

Files:

- `iac_bus.yaml` - declarative HTTP tool manifest.
- `iac_bus.py` - standard-library Python adapter and CLI.

The skill reads `IAC_BUS_URL` and `IAC_BUS_TOKEN` by default. `IAC_BUS_TOKEN`
maps to the server-side `BUS_API_TOKEN` bearer token.

## Direct CLI smoke test

```bash
IAC_BUS_URL=http://127.0.0.1:8101 \
IAC_BUS_TOKEN="$BUS_API_TOKEN" \
python3 skills/openclaw/iac_bus.py health
```

Post a progress event:

```bash
IAC_BUS_URL=http://127.0.0.1:8101 \
python3 skills/openclaw/iac_bus.py progress \
  --session-id demo \
  --sender agent.cursor.demo \
  --message "Working on the task"
```

## Import from an agent runtime

```python
from skills.openclaw.iac_bus import IacBusClient

bus = IacBusClient("http://127.0.0.1:8101", token="...")
bus.session_progress("demo", "agent.cursor.demo", "Starting")
claim = bus.claim_queue("work", "agent.cursor.demo")
```

See `../../docs/OPENCLAW_SKILL.md` for the full usage guide.
