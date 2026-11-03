# MCP setup: two Claude Code sessions on one repo

`mcp_server.py` is a stdio MCP server that turns the bus HTTP API into tools a
coding agent can call: path locks, work queues, messages, decisions and status.
Each agent session runs its own copy, with its own `BUS_AGENT_ID`.

This guide registers it for two Claude Code sessions, **agent A** and
**agent B**, working on the same repo.

## 1. Prerequisites

- A running bus that both sessions can reach. Locally:

  ```bash
  cd /path/to/iac-bus
  python3 -m venv venv
  ./venv/bin/pip install -r requirements.txt -r requirements-mcp.txt
  BUS_PORT=8101 BUS_API_TOKEN=devtoken ./venv/bin/python server.py
  ```

  Run a single bus process. Locks live in that process's memory.
- The MCP SDK on the machine that runs the agents:
  `pip install -r requirements-mcp.txt` (it pins `mcp>=2.3,<3`).
- Check the adapter can reach the bus before involving Claude Code:

  ```bash
  BUS_URL=http://127.0.0.1:8101 BUS_API_TOKEN=devtoken BUS_AGENT_ID=claude.a \
    /path/to/iac-bus/venv/bin/python /path/to/iac-bus/mcp_server.py --check
  ```

  It prints the resolved agent id, repo scope and bus URL, then pings
  `/health` and `/bus/locks`.

## 2. Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `BUS_URL` | `http://127.0.0.1:8091` | Bus base URL |
| `BUS_API_TOKEN` | empty | Bearer token, if the bus has one |
| `BUS_AGENT_ID` | `<user>@<host>` | This agent's name on the bus |
| `BUS_AGENT_SUFFIX` | `auto` | `auto` appends `#<6 hex>` per process; `0` keeps the name exact |
| `BUS_REPO` | origin remote's repo name | Lock scope. Every clone and worktree of a repo shares it |
| `BUS_REPO_ROOT` | git top level of the working directory | Used to turn absolute paths into repo-relative ones |
| `BUS_HTTP_TIMEOUT` | `15` | Seconds per HTTP call, on top of any wait |

**Why the suffix.** Locks held by the same identity never conflict, so an
agent can lock `src/` and then `src/app.py`. If two sessions shared one
identity, for example through a committed `.mcp.json`, they could edit the same
file at once. The random suffix makes every session distinct even when the
configuration is identical. With the suffix, the bus sees `claude.a#3f2a1c`, and
the session's own id is shown in `list_locks` and in the server instructions.

## 3. Give each session its own checkout

Two agents editing one working copy share one git index, so their commits
interfere even when their files don't. Give each session a git worktree:

```bash
cd /path/to/your-repo
git worktree add ../your-repo-agent-b -b agent-b
```

Both worktrees resolve to the same lock scope (`BUS_REPO` defaults to the
origin remote's repo name), so their locks see each other.

## 4. Register the server for each session

### Option A: per-session local registration (simplest)

Local scope is stored per project directory, so each worktree gets its own
registration and its own `BUS_AGENT_ID`.

Agent A, in the main checkout:

```bash
cd /path/to/your-repo
claude mcp add iac-bus --scope local \
  -e BUS_URL=http://127.0.0.1:8101 \
  -e BUS_API_TOKEN=devtoken \
  -e BUS_AGENT_ID=claude.your-repo.a \
  -- /path/to/iac-bus/venv/bin/python /path/to/iac-bus/mcp_server.py
claude
```

Agent B, in the worktree:

```bash
cd /path/to/your-repo-agent-b
claude mcp add iac-bus --scope local \
  -e BUS_URL=http://127.0.0.1:8101 \
  -e BUS_API_TOKEN=devtoken \
  -e BUS_AGENT_ID=claude.your-repo.b \
  -- /path/to/iac-bus/venv/bin/python /path/to/iac-bus/mcp_server.py
claude
```

### Option B: one shared `.mcp.json`, identity from the shell

Commit one project-scoped config and pass the identity when starting each
session. `${VAR:-default}` is expanded by Claude Code. Keep the token out of
the file:

```json
{
  "mcpServers": {
    "iac-bus": {
      "type": "stdio",
      "command": "/path/to/iac-bus/venv/bin/python",
      "args": ["/path/to/iac-bus/mcp_server.py"],
      "env": {
        "BUS_URL": "${BUS_URL:-http://127.0.0.1:8101}",
        "BUS_API_TOKEN": "${BUS_API_TOKEN}",
        "BUS_AGENT_ID": "${BUS_AGENT_ID:-claude}"
      }
    }
  }
}
```

```bash
# terminal 1
cd /path/to/your-repo && BUS_API_TOKEN=devtoken BUS_AGENT_ID=claude.a claude
# terminal 2
cd /path/to/your-repo-agent-b && BUS_API_TOKEN=devtoken BUS_AGENT_ID=claude.b claude
```

Claude Code asks once before it trusts a project-scoped server. Even if both
sessions start without `BUS_AGENT_ID`, the per-process suffix keeps them
distinct.

### Check it

Inside each session, `/mcp` should list `iac-bus` as connected with 13 tools.
From a shell, `claude mcp list` and `claude mcp get iac-bus` show the
registration.

## 5. Tell the agents the rules

The tool descriptions and the server instructions already tell agents to lock
before editing. Repeating it in the repo's `CLAUDE.md` makes it stick:

```markdown
## Working alongside other agents (iac-bus)

- Before editing, creating, renaming or deleting files, call `acquire_lock` on the
  narrowest path that covers the change. If it is refused, do not edit those files.
- Call `check_lock` right before committing, and `release_lock` right after.
- Use `request_decision` before deleting code, changing a public API or schema,
  merging or deploying, and wait for the answer.
- `report_status` when you start, get blocked, and finish. Release all locks when done.
```

## 6. Watch and steer

```bash
# who holds what
curl -s "http://127.0.0.1:8101/bus/locks?repo=your-repo" -H "Authorization: Bearer devtoken"
# lock events and agent status
curl -s "http://127.0.0.1:8101/bus/messages?channel=locks"  -H "Authorization: Bearer devtoken"
curl -s "http://127.0.0.1:8101/bus/messages?channel=status" -H "Authorization: Bearer devtoken"
```

Answer a `request_decision` (the agent shows the `conversation_id`; requests
are on channel `decisions`):

```bash
curl -s -X POST http://127.0.0.1:8101/bus/messages \
  -H "Authorization: Bearer devtoken" -H "Content-Type: application/json" \
  -d '{"channel":"decisions","sender":"lehel","type":"provide_decision",
       "conversation_id":"<conversation_id>",
       "message":{"kind":"response","action":"provide_decision",
                  "conversation_id":"<conversation_id>",
                  "payload":{"choice_id":"continue","approved":true,"notes":"go ahead"}}}'
```

Hand an agent a task through a queue:

```bash
curl -s -X POST http://127.0.0.1:8101/bus/messages \
  -H "Authorization: Bearer devtoken" -H "Content-Type: application/json" \
  -d '{"channel":"work","queue":"your-repo-tasks","sender":"lehel",
       "message":{"task":"Add input validation to src/api/users.py"}}'
```

Then tell the agent to `claim_work` from queue `your-repo-tasks`.

## 7. Behaviour to know

- When a session ends, the adapter releases every lock it still holds.
  If the process is killed, its locks expire after their `ttl_seconds`
  (default 600 through the adapter).
- `acquire_lock` and `read_messages` wait at most 30 seconds per call.
- A lock does not stop a file write; it is a promise between cooperating
  agents. `check_lock` before commit catches a lock that expired mid-edit.
- Identity is the name each adapter sends, trusted under the shared token.
  Per-run tokens, where the bus derives the identity, are the next step.

## 8. Try it without Claude Code

`bash scripts/demo_two_agents.sh` starts a throwaway bus, spawns two adapter
processes and drives them over MCP stdio, printing the hand-over transcript.
