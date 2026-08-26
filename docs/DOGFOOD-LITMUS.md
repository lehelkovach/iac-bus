# Dogfood + litmus (parallel coordination)

Use the bus to run **non-blocking parallel work**, then force a **synchronization barrier**
as a litmus test that coordination reconverges (not only fans out).

## When to run

- After L0/L1 changes that touch messaging, queues, or orchestration.
- Periodically during longer ladder builds (background agent OK).
- Against local `:8101` first; against `http://129.153.192.75:8101` or
  `https://iac-bus.knowshowgo.com` only when token + DNS/TLS allow.

## Litmus shape

```
          ┌── worker-a (nonblocking slice) ──┐
bootstrap ┤                                  ├── barrier-sync ── done
          └── worker-b (nonblocking slice) ──┘
```

Acceptance:

1. Bootstrap completes and posts a lifecycle event on channel `dogfood`.
2. Both workers claim assignments, finish scoped work, post `orchestration.step.status` completed.
3. Barrier step becomes ready **only after both** workers complete (`wait_for` / barrier).
4. Barrier agent posts `litmus-pass` on `dogfood` with job_id + step evidence.
5. `GET /bus/orchestration/jobs/<id>/ready` returns `[]`.

Failure modes that mean litmus **failed**:

- Barrier runs before both workers complete.
- Job left with non-terminal steps.
- Queue lease ack with wrong worker id (lease owner mismatch).

## Script

```bash
# Local (server already running)
BUS_URL=http://127.0.0.1:8101 BUS_API_TOKEN=devtoken bash ./scripts/dogfood_litmus.sh

# OCI edge (needs real token; do not commit it)
BUS_URL=http://129.153.192.75:8101 BUS_API_TOKEN=… bash ./scripts/dogfood_litmus.sh
```

Optional: spawn Cursor Task workers that claim from `orchestration` instead of the
script’s inline curl workers — that is the nested-agent dogfood path.

## Channel / identity conventions

- Channel: `dogfood`
- Queue: `orchestration`
- Handles: `agent:cursor.iac-bus.litmus-0@cloud` (orchestrator),
  `…litmus-a@cloud`, `…litmus-b@cloud`, `…litmus-barrier@cloud`

## Relation to KeyChain

None. Litmus is pure bus coordination. Do not pull KeyChain into this test.
