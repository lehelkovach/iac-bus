# A2A walkthrough with curl

`scripts/a2a_curl_demo.sh` runs this sequence against any bus. Below is a real
run against a local bus (2026-11-03); ids differ per run and the run token is
truncated.

```bash
BUS_PORT=8101 BUS_API_TOKEN=devtoken python server.py          # terminal 1
BUS_URL=http://127.0.0.1:8101 BUS_API_TOKEN=devtoken bash scripts/a2a_curl_demo.sh
```

What it does:

1. Reads the bus's public Agent Card.
2. The operator registers an external agent by its card, granting it one
   skill, one queue, one channel and one lock path. The bus mints the agent's
   run token; the shared token never leaves the operator.
3. The partner sends a task with `SendMessage`. It becomes a work item on
   `partner-inbox` that carries the A2A task id.
4. A queue it was not granted is refused with `PERMISSION_DENIED` (-32000).
5. A coding agent claims the item and asks a question with
   `request_decision`, linked to the work item. The partner's `GetTask` shows
   `TASK_STATE_INPUT_REQUIRED` with the question.
6. The partner answers with `SendMessage` on the same `taskId`; the bus posts
   it as `provide_decision` on the question's conversation and the task is
   working again.
7. The coding agent acks with a `result`; the partner sees the task
   `COMPLETED` with the result as its artifact.
8. `ListTasks` only ever shows the caller's own tasks.

```
== 1. Discover: the bus's Agent Card (public)
{'name': 'iac-bus', 'interface': {'protocolBinding': 'JSONRPC', 'protocolVersion': '1.0', 'url': 'http://127.0.0.1:8101/a2a'}, 'skills': ['enqueue-work', 'run-job'], 'auth': ['runToken']}

== 2. Operator registers an external agent by its card, with deny-by-default grants
{'handle': 'partner-agent', 'actor': 'partner-agent#d11095', 'grants': {'channels': ['a2a.partner'], 'lock_paths': ['docs'], 'queues': ['partner-inbox'], 'skills': ['enqueue-work']}, 'outbound_queue': 'a2a.partner-agent', 'run_token': 'iacrun_75i...'}

== 3. The partner sends a task (SendMessage, returnImmediately)
{'id': 'b502483b247442719600309330584e61', 'state': 'TASK_STATE_SUBMITTED', 'queue': 'partner-inbox'}

== 4. Grants hold: a queue it was not granted is refused
{'code': -32000, 'data': [{'@type': 'type.googleapis.com/google.rpc.ErrorInfo', 'domain': 'iac-bus', 'reason': 'PERMISSION_DENIED'}], 'message': "this agent may not use queue 'deploys'"}

== 5. A coding agent (run token) claims the work and asks a question
{'state': 'TASK_STATE_INPUT_REQUIRED', 'question': 'Include the breaking API change in the notes?\nOptions: Yes, No'}

== 6. The partner answers on the same task; the bus relays it as a decision
TASK_STATE_WORKING
('provide_decision', 'conv-demo', {'approved': True, 'decision': 'yes'})

== 7. The worker finishes and acks with a result; the partner sees the artifact
{'state': 'TASK_STATE_COMPLETED', 'artifact': 'Wrote docs/RELEASE.md (0.3.0, includes the breaking change)'}

== 8. ListTasks shows only the partner's own tasks
{'totalSize': 1, 'states': ['TASK_STATE_COMPLETED']}
```

The raw requests are in the script. Every call to `/a2a` sends
`Authorization: Bearer <run token>`, `A2A-Version: 1.0` and a JSON-RPC 2.0
body, for example:

```json
{"jsonrpc": "2.0", "id": 1, "method": "SendMessage", "params": {
  "message": {"messageId": "m1", "role": "ROLE_USER", "parts": [{"text": "Draft release notes for 0.3.0"}]},
  "configuration": {"returnImmediately": true},
  "metadata": {"queue": "partner-inbox", "lockPaths": ["docs/RELEASE.md"]}}}
```

## Outbound: the bus hands work to an external agent

Work posted to a registered agent's `outbound_queue` (`a2a.<handle>`) is
delivered by `scripts/a2a_bridge.py`:

```bash
BUS_URL=... BUS_API_TOKEN=... PARTNER_AGENT_TOKEN=... python scripts/a2a_bridge.py
```

The bridge locks the item's `lock_paths`, sends it to the agent's JSON-RPC
endpoint from its card (with the token from the env var named at
registration), relays the agent's questions as bus decisions, and acks the
item with the agent's artifacts. `tests/test_a2a_bridge.py` runs this against
a stub agent, including a task that arrives over A2A, is routed to the
external agent and comes back.
