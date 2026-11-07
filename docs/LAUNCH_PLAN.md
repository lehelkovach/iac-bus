# Launch plan: is iac-bus useful, and how to find out in public

_Written 2026-11-07. Owner decisions are marked **(owner)**._

## Is it useful? An honest read

What it does that nothing else in the stack does, as of today:

- **Cross-tool locks with fencing.** Claude Code's own parallelism (worktrees,
  subagents) avoids conflicts by giving each agent a separate checkout; it does
  not stop two agents from editing the same file in the same checkout, and it
  cannot coordinate agents from different vendors. The bus does both, and the
  pre-commit hook makes the lock a gate rather than a suggestion. The
  lost-update benchmark is the clearest demonstration: 75% of writes lost
  without it, 0% with it.
- **A human approval loop that works across sessions.** `request_decision`
  plus the status page is a small thing, but there is no standard way today
  for an agent in one terminal to ask a person who is watching another.
- **Identity the server assigns.** Run tokens and grants mean "agent B cannot
  release agent A's lock" is enforced, not hoped for.
- **A2A both ways**, so an outside agent can hand work to supervised coding
  agents and get a result back. Built and tested; no outside agent uses it yet.

What is true and should be said plainly:

- It is one Python process with a SQLite file. That is fine for a team and
  wrong for a platform. Say so.
- Locks are per repo path, not semantic. Two agents editing different files
  that must change together are not protected.
- The agents must be told about the tools; the adapter's instructions do that,
  and the trial shows Sonnet follows them, but a model that ignores its tool
  descriptions ignores the bus. The hook catches the commit, not the edit.
- Every number in `docs/BENCHMARKS.md` comes from one machine and one model.

Verdict: it is useful for exactly one situation, several coding agents
sharing a repository with a human in the loop, and that situation is becoming
common. It is not a general agent framework and should not be pitched as one.

## Before anyone else can use it

| Item | Status | Owner |
| --- | --- | --- |
| A LICENSE file. Without one nobody may legally use the code. Apache-2.0 fits an infrastructure project (patent grant); MIT is the simplest. | **missing** | **(owner)** |
| Installable with one command | done (`pip install "iac-bus[mcp] @ git+..."`) | |
| 5-minute quickstart in the README | done | |
| Two-session Claude Code guide, Cursor config | done (`docs/MCP_SETUP.md`, `examples/mcp.json`) | |
| Benchmarks with reproducible commands | done (`docs/BENCHMARKS.md`) | |
| Real-work evidence | done (`docs/DOGFOOD-2026-11-07.md`, `docs/RUNNER_DEMO.md`) | |
| CI that runs on pull requests | manual-only (Actions budget spent) | **(owner)**: restore the budget, or move CI to a free runner |
| A hosted demo bus people can try | exists (HTTPS, OCI) but token-gated; see "Demo" | **(owner)**: decide whether to hand out tokens |
| Release tagged on `master` (`v0.2.0`) | pending the `dev` promotion | |
| A 90-second demo recording (asciinema or GIF) | not made | |

## The demo (90 seconds, terminal only)

Script, from a checkout with the bus running:

1. `bash scripts/demo_two_agents.sh` (20 s): two MCP server processes, A locks
   `src/app.py`, B is refused, B waits, A releases, B gets a higher fencing
   token, A's old token is now stale. The transcript is self-explanatory.
2. `python scripts/bench.py` (15 s): the lost-update table scrolls past, 75%
   then 0%.
3. Two real Claude Code sessions in two worktrees (the rest): give both the
   same instruction, "add a function to src/app.py", at the same time. Show
   `/ui` in a browser while they run: one lock, one agent waiting, then both
   changes in the file. Then tell one to delete a file; show the decision
   appear in `/ui`; press Deny; show the agent's reply.

Record it once with asciinema (steps 1 and 2) and a screen recording (step 3).
Put the GIF at the top of the README.

## Where to show it

In order of expected signal per hour spent:

1. **The MCP ecosystem lists.** The official MCP registry
   (registry.modelcontextprotocol.io), `awesome-mcp-servers`, Smithery, Glama.
   These are where people looking for "an MCP server for X" look. iac-bus is
   unusual there: most servers wrap an API; this one coordinates the agents
   themselves. Lead with that.
2. **One written piece with the benchmark.** A post titled on the lost-update
   number ("Four coding agents, one file: 75% of writes lost") with the
   benchmark, the hook, and the trial. Post it to Hacker News (Show HN),
   r/ClaudeAI, r/LocalLLaMA (A2A angle) and the Anthropic developer Discord.
   The number does the work; keep the post short and link the repo.
3. **The A2A community.** The A2A project tracks implementations; an issue or
   PR adding iac-bus to its list of compatible servers is cheap and reaches
   the people who have an outside agent to connect.
4. **A talk or a thread, later,** once there is one outside user with a story.

What not to do: claim it scales, claim it is secure beyond a shared team, or
launch without the license and the recording.

## How to tell if it worked

- Week 1: 50 stars and 3 issues from people who ran it. Issues matter more
  than stars; they mean someone tried it.
- Week 4: one repository other than yours using the hook or the runner, and
  one A2A registration from an agent you did not write.
- If after a month nobody outside has run it, the honest conclusion is that
  the single-checkout multi-agent case is rarer than it looks, and the right
  move is to fold the useful parts (the hook, the approval page) into the
  tools people already use rather than keep a separate bus.

## Roadmap after launch, only on demand

- Streamable HTTP MCP served by the bus itself (no local adapter install).
- Orchestration jobs in the durable store; barriers.
- Postgres and multi-process serving, only when a user has more than one team.
- Grants with signatures (Track B), only when agents from outside your trust
  boundary join.
