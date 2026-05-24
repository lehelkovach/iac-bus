# Inter-Agent Communication Bus (IAC Bus)

Lightweight HTTP message bus for coordinating multiple agents.

## Table of Contents
- [Overview](#overview)
- [Features](#features)
- [Architecture](#architecture)
- [Quick Start](#quick-start)
- [Configuration](#configuration)
- [API](#api)
- [Deployment](#deployment)
- [Scripts](#scripts)
- [Development](#development)
- [Testing](#testing)
- [Roadmap](#roadmap)
- [Documentation](#documentation)
- [Planning and Scaffolding](#planning-and-scaffolding)
- [Contributing](#contributing)
- [Security](#security)
- [License](#license)

## Overview
The IAC Bus provides a small HTTP API for posting and polling messages between
agents. Messages are stored in memory with retention limits to keep the service
simple to deploy and restart.

## Features
- Simple REST endpoints for posting and polling messages
- In-memory retention with size and time limits
- Optional bearer-token authentication
- Systemd deployment via `deploy.sh`
- Utility scripts for hotfixes and agent lifecycle

## Architecture
- Single-process, in-memory message store
- Automatic pruning by age and max message count
- Health endpoint without authentication

## Quick Start
```bash
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
BUS_API_TOKEN=devtoken ./venv/bin/python server.py
```

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `BUS_HOST` | `0.0.0.0` | Bind address |
| `BUS_PORT` | `8091` | Listen port |
| `BUS_API_TOKEN` | empty | Bearer token |
| `BUS_MAX_MESSAGES` | `500` | Max retained messages |
| `BUS_RETENTION_SECONDS` | `3600` | Message retention window |
| `BUS_LOG_LEVEL` | `INFO` | Log level |

## API
Endpoints:
- `POST /bus/messages`
- `GET /bus/messages`
- `GET /health`

See [DOCUMENTATION.md](DOCUMENTATION.md) for request and response details.

## Deployment
```bash
sudo ./deploy.sh
sudo systemctl status iac-bus.service
```

The deploy script installs to `/opt/iac-bus` and creates
`/etc/iac-bus/iac-bus.env` for configuration.

## Scripts
- `scripts/hotfix-pull.sh` applies a hotfix from GitHub on a VM.
- `scripts/hotfix-remote.sh` applies a hotfix over SSH from your machine.
- `scripts/spawn-cursor-agents.py` spawns Cursor agents and announces them on the bus.
- `scripts/handoff-terminate-agents.py` terminates Cursor agents by ID, file, or bus.

Examples:
```bash
sudo ./scripts/hotfix-pull.sh
./scripts/hotfix-remote.sh ubuntu@<IP> ~/.ssh/your_key
```

```bash
export CURSOR_API_KEY="key_xxx..."
python3 scripts/spawn-cursor-agents.py \
  --cursor-endpoint "https://api.cursor.com/v0/agents" \
  --count 2 \
  --payload '{"name":"agent-a"}' \
  --bus-url "http://127.0.0.1:8091" \
  --bus-token "$BUS_API_TOKEN"
```

## Development
```bash
python3 -m venv venv
./venv/bin/pip install -r requirements.txt -r requirements-dev.txt
```

## Testing
```bash
./venv/bin/pytest
```

## Roadmap
- Identity and presence tracking
- Roles and hierarchy for supervisor/subordinate agents
- Directed routing and group addressing
- Work leasing and acknowledgements
- Conversation threads
- Access control and channel ACLs
- Optional persistence (SQLite/Postgres/Redis)
- Streaming (SSE or WebSocket)
- Rate limiting

## Documentation
Detailed documentation lives in [DOCUMENTATION.md](DOCUMENTATION.md).

## Planning and Scaffolding
Implementation planning and execution scaffolding are documented in:

- [docs/ACP_DEV_PLAN.md](docs/ACP_DEV_PLAN.md) - consolidated MVP-first development plan with OSS architecture references.
- [docs/ROADMAP.md](docs/ROADMAP.md) - versioned delivery roadmap from easiest to most complex.
- [docs/TESTING_STRATEGY.md](docs/TESTING_STRATEGY.md) - TDD/BDD cascading test strategy and completion gates.
- [prompts/README.md](prompts/README.md) - agent prompt file conventions and usage.

## Contributing
Open an issue or submit a pull request. Please include tests for new behavior.

## Security
If you expose the bus publicly, configure `BUS_API_TOKEN` and run behind TLS
or a trusted network boundary.

## License
No license file is present. Add a LICENSE file to define usage terms.
