# Runner end to end: an A2A task done by a real Claude Code session

`scripts/agent_runner.py` is the piece that wakes agents: it claims work from a
bus queue, starts a Claude Code session for each item with its own run token
and the MCP adapter, and acks the item with Claude's final reply.

This is a real run (2026-11-05) against the OCI dev bus over HTTPS
(`https://129-146-105-237.sslip.io`), driven from a Claude Code cloud session:

1. The operator registers a partner agent whose only grant is `enqueue-work`
   on `repo-tasks`.
2. The partner sends a coding task over A2A.
3. The runner claims it and runs `claude -p` (Sonnet) in a scratch git repo
   with the adapter configured with a fresh run token and the item id.
4. The partner reads the finished task and its artifact over A2A.

```
== 1. Operator registers a partner agent that may only enqueue work on repo-tasks
{"handle": "release-bot", "actor": "release-bot#b2ba83", "grants": {"channels": [], "lock_paths": [], "queues": ["repo-tasks"], "skills": ["enqueue-work"]}}

== 2. The partner sends a task over A2A
{"id": "3545ef8e954942dcb2e036cb8cc4f80f", "state": "TASK_STATE_SUBMITTED", "queue": "repo-tasks"}

== 3. The runner claims it and starts a Claude Code session (real claude -p, Sonnet)
[runner] running as runner#160650; queues ['repo-tasks']; workdir <scratch repo>
[runner] 47c68839b15b4b41b3d7a48f0442eed2: starting runner.agent#248fa0 for 'In src/text.py add a function slugify(text) that lowercases the text, '
[runner] 47c68839b15b4b41b3d7a48f0442eed2: acked: "I added `slugify` and its tests, but I haven't run any of them. `pytest` isn't installed here, and both the install and "
processed 1 item(s)
(runner finished in 29s, exit 0)

== 4. The partner reads the result over A2A
{"state": "TASK_STATE_COMPLETED"}
artifact: I added `slugify` and its tests, but I haven't run any of them. `pytest` isn't installed here, and both the install and a plain-Python fallback run were blocked pending approval. Please run `python3 -m pytest` once pytest is available.

- **`src/text.py`:** `slugify(text)` lowercases and trims the text, then replaces each run of non-alphanumeric characters with a single `-`, with no leading or trailing `-`. It uses `re.sub(r"[^a-z0-9]+", "-", ...)`. Because the pattern is ASCII-only, accented letters and non-Latin letters are treated as separators.
- **`tests/test_text.py`:** I kept `test_shout` and added five slugify tests: basic input, trimming and collapsing runs, no edge dashes, digits, and input with no letters or digits (returns `""`).

I held locks on both files while editing and released them afterwards. I didn't commit.

== 5. What the bus saw
  lock.acquired  src/text.py          runner.agent#248fa0
  lock.acquired  tests/test_text.py   runner.agent#248fa0
  lock.released  src/text.py          runner.agent#248fa0
  lock.released  tests/test_text.py   runner.agent#248fa0
  status running    runner#160650              started runner.agent#248fa0 for work 47c68839b15b4b41b3d7a48f0442eed2 on repo-tasks
  status running    runner.agent#248fa0        Adding slugify to src/text.py with tests
  status completed  runner.agent#248fa0        slugify and tests written; tests NOT run (pytest not installed, install/run needed approva
  status completed  runner#160650              work 47c68839b15b4b41b3d7a48f0442eed2 done: I added `slugify` and its tests, but I haven't
```

Notes:

- The agent locked both files under its own run (`runner.agent#...`) before
  editing and released them after. The runner then ended the agent's run.
- The agent did not run the tests: `pytest` was not installed for the system
  `python3` its permissions allowed, and it reported that instead of claiming
  success. Run afterwards with pytest available, its tests pass (6 passed).
  Give the agent the tools your repo needs with `--allow`, for example
  `--allow 'Bash(pytest:*)'`.
- The partner never held the shared token: its run token only allows
  `enqueue-work` on `repo-tasks`.

Reproduce:

```bash
BUS_URL=https://<bus> BUS_API_TOKEN=<operator token> \
  python scripts/agent_runner.py --queue repo-tasks --workdir /path/to/repo --model claude-sonnet-5-5 --once
```
