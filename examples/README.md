# Examples

- `mcp.json`: a project-scoped Claude Code MCP config. Copy it to your repo
  root as `.mcp.json`. It takes the bus token and the agent id from the shell
  that starts `claude`, so one file serves every session. Cursor and other
  MCP clients take the same `command` and `env`.
