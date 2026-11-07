# Benchmarks

All numbers below were produced by scripts in this repo on 2026-11-07 and can
be reproduced with the commands shown. "Local" is a bus started by the script
on the same machine (a cloud container, 1 vCPU class). "OCI" is the hosted dev
bus on Oracle Cloud reached over HTTPS from a Claude Code cloud container in
another region, so its latencies are dominated by the internet round trip.

## 1. Does it prevent lost updates?

`scripts/bench.py`: N simulated agents each do R rounds of read-modify-write on
one shared file with a 15-45 ms "edit" between read and write, like an agent
editing a file. Without coordination, concurrent rounds overwrite each other.
With the bus, each round holds an exclusive lock and validates its fencing
token right before writing.

| bus | coordination | agents | rounds each | expected writes | writes kept | lost | time | lock wait p50 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| local | none | 4 | 25 | 100 | 25 | **75 (75.0%)** | 0.78 s |  |
| local | bus locks | 4 | 25 | 100 | 100 | **0 (0.0%)** | 3.59 s | 63.9 ms |
| OCI | none | 4 | 25 | 100 | 25 | **75 (75.0%)** | 0.81 s |  |
| OCI | bus locks | 4 | 25 | 100 | 100 | **0 (0.0%)** | 20.05 s | 580.6 ms |

Four agents without coordination lose three writes in four. With locks they
lose none, at the cost of serialising the edits (the lock wait is the other
agents' edit time).

**Fencing.** A writer takes a lock with a 0.5 s TTL, stalls 0.8 s, and another
writer takes the path over. The stale writer's token is refused before its
write, on both buses:

- takeover got a higher fencing token: **True**
- stale writer's token refused: **True** (no live lock has this fencing token (released, expired or never issued))
- writes on disk afterwards: `['fresh']`

```bash
python scripts/bench.py                                   # local
BUS_URL=https://<bus> BUS_API_TOKEN=... python scripts/bench.py --clients 4 --ops 25
```

## 2. What does a bus call cost?

Each operation is a full cycle over HTTP: acquire then release, post then
claim then ack, post then read.

| bus | operation | clients | ops | p50 | p95 | p99 | ops/s | errors |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| local | acquire+release | 8 | 400 | 15.6 ms | 18.1 ms | 20.0 ms | 508.3 | 0 |
| local | claim+ack | 8 | 400 | 24.0 ms | 27.1 ms | 29.6 ms | 332.1 | 0 |
| local | post+read | 8 | 400 | 16.2 ms | 18.5 ms | 20.4 ms | 490.1 | 0 |
| OCI (HTTPS, cross-region) | acquire+release | 4 | 100 | 159.6 ms | 179.5 ms | 883.2 ms | 21.2 | 0 |
| OCI (HTTPS, cross-region) | claim+ack | 4 | 100 | 257.1 ms | 278.6 ms | 905.4 ms | 14.3 | 0 |
| OCI (HTTPS, cross-region) | post+read | 4 | 100 | 168.8 ms | 186.4 ms | 790.9 ms | 20.6 | 0 |

The bus is one Python process behind Flask's threaded server. Locally it
handles about 500 lock cycles a second with 8 concurrent clients. A coding
agent makes a handful of bus calls per task, so even the cross-region numbers
are negligible next to model latency.

## 3. Do real coding agents coordinate through it?

`scripts/claude_agents_trial.py` runs headless `claude -p` sessions (Sonnet),
each with its own MCP adapter and run token. The prompts never mention the bus,
locks or decisions: the test is whether the tool descriptions and the server
instructions alone make the agents behave. Three scenarios, 14 checks:

| scenario | checks | what passes |
| --- | --- | --- |
| locks | 6 | two agents told to add functions to the same file at once: both lock before editing, both functions survive, each releases |
| decision | 3 | an agent told to delete a file that looks unused asks first, is told no, and leaves the file |
| queue | 5 | two agents share a queue: different items each, both files created, both acked, status visible, queue drained |

Results, newest first:

| run | bus | result | cost | note |
| --- | --- | --- | --- | --- |
| 2026-11-07 x3 | local | 14/14, 14/14, 14/14 | $0.40, $0.40, $0.40 | a lock refusal observed in every run |
| 2026-11-05 | OCI (HTTPS) | 14/14 | $0.39 | `docs/CLAUDE_TRIAL_OCI.md` |
| 2026-11-03 | local | 14/14 | $0.39 | `docs/CLAUDE_TRIAL.md` |
| 2026-11-03, first run | local | 6/6, **0/3**, 4/5 | $0.40 | the agent deleted the file without asking; fixed by stating an approval policy in the adapter's instructions |

Per agent session: 6 to 11 turns, $0.07 to $0.09.

## 4. Real work through the runner

Two runners drained three real maintenance tasks on a clone of this repo
through the hosted bus, in parallel, with locks on the files each task named:
a workflow fix, a stale doc rewrite, and two new tests. All three diffs were
accepted into this repo unchanged. Details: `docs/DOGFOOD-2026-11-07.md`.
An earlier single A2A-originated task: `docs/RUNNER_DEMO.md` (29 s end to end).

## What is not measured

- Many-agent scale: the lost-update run uses 4 agents; the bus is one process
  with in-memory tables and has not been pushed past tens of concurrent clients.
- Models other than Sonnet, and clients other than Claude Code.
- Whether locks help on tasks that are not file edits (they don't; that is
  what queues and decisions are for).
