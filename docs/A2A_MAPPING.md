# A2A mapping

_Written 2026-11-03. Status: **implemented** (Phase 4, approved 2026-11-03 with the recommended answers to §7). Section 8 lists what the code does where it differs from or adds to this design. Walkthrough: `docs/A2A_DEMO.md`._

This maps the Agent2Agent protocol (A2A) onto iac-bus. It covers what each A2A concept becomes on the bus, what A2A lacks that the bus provides, what the bus would have to change, and a proposed Phase 4 scope with the decisions it needs.

## 1. Which A2A

| Item | Value | Source |
| --- | --- | --- |
| Released version | **1.0.0** (2026-03-12). 1.0.1 (late May 2026) changes only proto comments | [CHANGELOG](https://github.com/a2aproject/A2A/blob/main/CHANGELOG.md) |
| Wire version | `"1.0"`. Patch numbers are not used on the wire | spec §3.6 |
| Normative source | `specification/a2a.proto`. Where the prose disagrees, the proto wins | spec §1.4, [a2a.proto](https://github.com/a2aproject/A2A/blob/main/specification/a2a.proto) |
| Unreleased | `v1.1.0-rc` adds `Task.generation`, `if_generation_match` (409), `Task.timeline`, and a bidirectional stream | tag `v1.1.0-rc` |
| Governance | Linux Foundation; an Agentic AI Foundation hosted project since 2026-08-17 | [AAIF](https://aaif.io/blog/a2a-joins-aaif) |
| Python SDK | `a2a-sdk` 1.3.0. Its HTTP layer is Starlette only (async); no Flask support | [PyPI](https://pypi.org/project/a2a-sdk/), [a2a-python](https://github.com/a2aproject/a2a-python) |

What changed from 0.3, and why it matters here:

- **Discovery path.** The Agent Card lives at **`/.well-known/agent-card.json`** (spec §8.2). Phase 4's draft named `/.well-known/agent.json`, which is the older path.
- **Method names.** JSON-RPC methods were renamed to `SendMessage`, `SendStreamingMessage`, `GetTask`, `ListTasks` (new), `CancelTask`, `SubscribeToTask`, `Create/Get/List/DeleteTaskPushNotificationConfig(s)` and `GetExtendedAgentCard`.
- **Wire format.**
  - Wire JSON is camelCase (§5.5).
  - Enums are prefixed SCREAMING_SNAKE, such as `TASK_STATE_WORKING` and `ROLE_USER`.
  - Parts are one object holding exactly one of `text`, `raw`, `url` or `data`.
  - `SendMessage` returns `{"task": …}` or `{"message": …}`.
- **Card fields.** `url`, `preferredTransport` and `protocolVersion` became `supportedInterfaces[]{url, protocolBinding, protocolVersion}`. `security` became `securityRequirements`.
- **Version header.** Clients MUST send `A2A-Version: 1.0`. A server MUST treat a missing value as 0.3 and answer `VersionNotSupportedError` (-32009) for versions it lacks (§3.6).
- **Blocking.** `blocking` became `returnImmediately`. The default (`false`) blocks until the task reaches a terminal or interrupted state (§3.2.2).

## 2. Shape of the integration

A2A is a peer protocol. A **client** agent sends messages to a **remote** agent, which runs tasks and returns status and artifacts. Agents are opaque to each other (§1). The bus can sit on either side:

```
             inbound (Phase 4)                         outbound (Phase 4b)
A2A client ──SendMessage──▶ bus A2A endpoint     bus bridge worker ──SendMessage──▶ external A2A agent
                              │                          ▲
                              ▼                          │ claim
                   orchestration job / queue item ──▶ queue "a2a.<agent>"
                              │
                     our coding agents (MCP: claim_work, locks, decisions)
```

- **Inbound.** The bus publishes an Agent Card and accepts A2A tasks. Each task becomes an orchestration job or a queue item that our agents claim through MCP. Task state is derived from the job and step state.
- **Outbound.** An external A2A agent registered by its card URL joins supervised work. A bridge worker claims queue items addressed to it, sends them with `SendMessage`, and maps the returned task state back into the bus. When the external agent asks for input (`TASK_STATE_INPUT_REQUIRED`), that becomes a `request_decision` on the bus, so the supervisor or a human answers it like any other decision.

## 3. Concept map

### 3.1 Discovery and identity

| A2A | iac-bus | Notes |
| --- | --- | --- |
| Agent Card at `/.well-known/agent-card.json` | New public route serving the bus's own card | Clients fetch the card to learn how to authenticate, so it must be readable without a token. The route joins `/health` in the auth exemption. It holds no secrets. |
| `AgentCard.skills[]` | What the bus offers: run an orchestration job, enqueue work for our coding agents | Proposed skills are listed in §5 |
| `supportedInterfaces[]` | `{url: <bus>/a2a, protocolBinding: "JSONRPC", protocolVersion: "1.0"}` | JSON-RPC only at first; gRPC and REST are not planned |
| `securitySchemes` + `securityRequirements` | `httpAuthSecurityScheme {scheme: "Bearer"}`, satisfied by a **run token** | Phase 2b is a prerequisite. The shared token must never be handed to an external agent. |
| An external agent's card URL | Agent record + the stored card JSON + the roles we grant | "Store the card plus roles and permissions" is the whole registry for Phase 4. Principals and grants stay in Track B. |
| Identity of the caller | Run actor `<handle>#<run>` derived from the run token | A2A leaves identity to the transport ("handled at the protocol layer", §7) and says servers MUST authenticate every request (§7.4). Run tokens are exactly that layer. |
| `AgentCard.signatures` (JWS over the RFC 8785 canonical form) | Not verified | Matches the identity plan: signatures wait for a second verifier (B3) |
| `tenant` | Unused | Its semantics are undefined in the spec |

### 3.2 Messages, tasks and artifacts

| A2A | iac-bus | Notes |
| --- | --- | --- |
| `Message {messageId, role, parts, contextId?, taskId?, referenceTaskIds?, metadata?}` | Bus message `{id, sender, type, message, conversation_id, reply_to}` | `messageId` goes in `headers.a2a_message_id`, for idempotency. `role` follows from direction. |
| `Part.text` | `message` string | |
| `Part.data` | `message` object | |
| `Part.url` | A link inside the `message` object | Never fetched by the bus |
| `Part.raw` (base64) | Rejected with `ContentTypeNotSupported` (-32005) at first | The bus retains at most 500 messages, so it is no place for blobs |
| `contextId` | `conversation_id`, plus the orchestration `job_id` for job tasks | One context can hold several tasks |
| `Task {id, contextId, status{state, message, timestamp}, artifacts[], history[]}` | **New** in-memory `a2a_tasks` record linking the A2A task id to `job_id`, `step_id` or the queue `message_id`, plus caller, state and artifacts | A2A task ids are server-generated (§3.4.2). We mint them; clients never pick them. |
| `Artifact {artifactId, parts, name?}` | The result payload of a completed step, posted with the `agent_contract` action `report_result` | No such message exists yet; see change 4 in §5 |
| `SendMessage` without `taskId` | Create a single-step orchestration job (skill `run-job`) or enqueue a queue item (skill `enqueue-work`), then return `{"task": …}` in `SUBMITTED` | |
| `SendMessage` with the `taskId` of an `INPUT_REQUIRED` task | `provide_decision` / `provide_input` on channel `decisions`, sharing the request's `conversation_id` | The same envelope the MCP `check_decision` tool already reads |
| `GetTask {id, historyLength?}` | Look up `a2a_tasks`, then derive the state (§3.3) | |
| `ListTasks` | `a2a_tasks` filtered to the caller's run | Scoped to the caller, as §13.1 requires |
| `CancelTask` | Post a `cancel_task` control message to the assigned worker and mark the step cancelled | The orchestration layer has no cancelled status; see change 2 |
| `SendStreamingMessage`, `SubscribeToTask` | Could ride on the bus long-poll as SSE | Not at first: `capabilities.streaming = false` |
| Push-notification config methods | Not supported: `PushNotificationNotSupported` (-32003), `pushNotifications = false` | |
| `GetExtendedAgentCard` | `ExtendedAgentCardNotConfigured` (-32007) | |

### 3.3 Task state, both ways

The bus has three state vocabularies today:

| Source | Values |
| --- | --- |
| `orchestration.StepStatus` | `pending`, `running`, `completed`, `failed`, `skipped`, `timed_out` |
| `agent_contract.AgentState` | `idle`, `running`, `waiting_input`, `paused`, `completed`, `failed`, `cancelled`, `timed_out` |
| Queue item status | `pending`, `leased`, gone after `ack` |

**Bus to A2A** (inbound tasks, `GetTask`):

| Bus condition | A2A `TaskState` |
| --- | --- |
| Step `pending`, or queue item `pending` | `TASK_STATE_SUBMITTED` |
| Step `running`, or queue item `leased` | `TASK_STATE_WORKING` |
| Open `request_decision` for this task's conversation | `TASK_STATE_INPUT_REQUIRED` (the request becomes `status.message`) |
| Worker's last `report_status` is `paused` | `TASK_STATE_WORKING` (A2A has no paused state) |
| Step `completed`, or queue item acked with a result | `TASK_STATE_COMPLETED` |
| Step `failed` or `timed_out` | `TASK_STATE_FAILED` (reason in `status.message`) |
| Step `skipped` | `TASK_STATE_CANCELED` if the skip came from `CancelTask`, else `TASK_STATE_FAILED` |
| Rejected by role or policy before any work | `TASK_STATE_REJECTED` |
| Needs credentials | `TASK_STATE_AUTH_REQUIRED`: never produced at first |

**A2A to bus** (outbound tasks, the bridge):

| External `TaskState` | Bus effect |
| --- | --- |
| `SUBMITTED`, `WORKING` | Keep the queue lease, renew it, `report_status(running)` on behalf of the external agent |
| `INPUT_REQUIRED` | Post `request_decision` (the agent's `status.message` becomes the prompt) and keep the lease. The answer is sent back with `SendMessage` on the same `taskId`. |
| `AUTH_REQUIRED` | `request_decision` to the operator; the bridge never forwards credentials by itself |
| `COMPLETED` | Post the artifacts as the step result, mark the step `completed`, ack the item |
| `FAILED` | Mark the step `failed`, ack the item |
| `REJECTED` | `nack` (requeue = false) and mark the step `failed` with the reason |
| `CANCELED` | Mark the step `skipped`, ack the item |

Terminal A2A tasks are immutable. `SendMessage` to one gives `UnsupportedOperation`, and cancelling one gives `TaskNotCancelable` (-32002). The bridge must never try to resume them.

### 3.4 Coordination primitives

| iac-bus primitive | A2A equivalent | Consequence |
| --- | --- | --- |
| Path locks with fencing tokens | **None.** The spec has no locking, leases or concurrency control; optimistic concurrency appears only in the unreleased v1.1-rc | The bus takes locks **on behalf of** external agents (§4) |
| Queue claim, lease, ack and nack | None. "Messages MUST NOT be considered a reliable delivery mechanism" (§3.7) | The bridge owns retries. Idempotency comes from our `messageId`. |
| Orchestration DAG, barriers, time gates | None. `referenceTaskIds` only links related tasks | DAGs stay in the bus; A2A sees single tasks |
| `request_decision` / `provide_decision` | `TASK_STATE_INPUT_REQUIRED` plus a follow-up message | Clean 1:1 mapping |
| Supervisor and worker hierarchy (ordinal paths, `parent_agent_uuid`) | None. A2A is peer to peer | Kept in our metadata; never on the A2A wire |
| Run identity (Phase 2b) | None; deferred to the transport (§7) | Run tokens are our transport identity |
| Authority, grants and approvals (Track B) | None. Authorization "is implementation-specific" (§7.5); the spec "does not define the scope, representation, validity, or revocation semantics of the authorization decision" (§7.6.4) | Phase 4 needs a minimal roles model (§5) |
| Agent registry | None. "does not prescribe a standard API for curated registries" (topics/agent-discovery.md) | Card storage only, as scoped |
| Channels and broadcast messages | None. A2A is point to point | External agents cannot read channels unless granted |

## 4. What A2A lacks that we provide, and how external agents get it

1. **Locks.** An A2A agent is opaque and does not speak MCP, so it cannot call `acquire_lock`. The bridge does it for the agent:
   - A work item may declare the paths it will touch (`metadata.iac.lock_paths`).
   - Before `SendMessage`, the bridge acquires exclusive locks on those paths, as the external agent's run.
   - It passes the fencing tokens in message metadata (`metadata.iac.fencing_tokens`).
   - It renews the locks while the task is `WORKING` and releases them when the task reaches a terminal state.
   - If the agent pushes a commit, the write boundary (a pre-commit hook or CI calling `/bus/locks/validate`) checks the token.
   - Later, an A2A **extension** (`capabilities.extensions[]`, a URI such as `https://iac-bus.knowshowgo.com/ext/locks/v1`) could let lock-aware agents ask for locks themselves.
2. **Reliable work hand-off.** Queue leases and acks give at-least-once delivery that A2A does not. The bridge keeps the lease for as long as the external task is live.
3. **Hierarchy and supervision.** Supervisors assign work through queues and orchestration jobs. External agents only ever see A2A tasks. Their questions surface as bus decisions.
4. **Authority.** The roles stored with each registered card decide what that agent may receive and do (§5). A2A itself grants nothing.
5. **Audit.** Every bus action by an external agent is attributed to its run actor. A2A has no attribution of its own beyond transport auth.

## 5. What we would have to change

| # | Change | Why |
| --- | --- | --- |
| 1 | `/.well-known/agent-card.json` exempt from auth. Optionally serve `/.well-known/agent.json` as an alias | Discovery must be public. The alias only helps older clients that still use the earlier path. |
| 2 | Add `cancelled` and `waiting_input` to `orchestration.StepStatus`, or keep them in the A2A task record | `CancelTask` and `INPUT_REQUIRED` have nowhere to live today. `skipped` is ambiguous. |
| 3 | In-memory `a2a_tasks` table (task id, context id, caller run, job, step or queue message, state, artifacts, history). Same pattern as locks and runs | `GetTask`, `ListTasks` and idempotent `SendMessage` need it |
| 4 | A result message: workers post `report_result` (already an `AgentAction`) with artifacts when they complete a step. The MCP adapter gains a `report_result` tool | A2A clients expect artifacts. Steps today only carry a status. |
| 5 | Converters at the boundary: snake_case and camelCase; epoch floats and ISO 8601 UTC with milliseconds; enum prefixes; Part and message | The wire format differs from our JSON everywhere |
| 6 | `A2A-Version` header check: accept `1.0`; answer -32009 otherwise, including a missing header (which means 0.3) | Required by §3.6 |
| 7 | Roles on runs: `roles[]` and an allowlist (`skills`, `queues`, `channels`, `lock_paths`) set when an external agent is registered, checked by the A2A endpoint and the bridge | The scoped "roles/permissions we grant". It is a deliberately small subset of Track B grants. |
| 8 | `POST /agents/a2a/register {card_url, handle, roles, …}`: fetch the card over HTTPS with a timeout and size cap, validate the required fields, store it, mint a run token for the agent | Fetching an operator-supplied URL is an SSRF risk: https only, no private addresses unless allowed for dev |
| 9 | Blocking `SendMessage`: honour `returnImmediately: true`. For the default, block up to `BUS_WAIT_SECONDS_MAX` (30 s) and then return the task still `WORKING` | Coding tasks take minutes to hours. Holding a request that long is not workable. This deviates from §3.2.2's "blocks until terminal or interrupted" and is decision A below. |
| 10 | Credentials for outbound calls stored by **name** only: the card's `securitySchemes` say what is needed, and an env var holds the value | `AGENTS.md`: never print secrets; record secret names only |

Nothing in `agent_contract.py` has to change shape. `REQUEST_DECISION`, `PROVIDE_DECISION`, `CANCEL_TASK`, `REPORT_STATUS` and `REPORT_RESULT` already exist. Mapping `AgentState` to `TaskState` is a pure function.

## 6. Proposed Phase 4 scope

**4a. Inbound.**
- `GET /.well-known/agent-card.json`. A card validated in tests by parsing it with the `a2a-sdk` protobuf types (a test-only dependency).
- `POST /a2a`, JSON-RPC 2.0, implementing `SendMessage`, `GetTask`, `ListTasks` and `CancelTask`. The other methods return the spec error codes.
- Two skills:
  - `run-job`: a task becomes a single-step orchestration job on a named queue.
  - `enqueue-work`: a task becomes a queue item for our coding agents.
- Changes 1–7 and 9.
- `POST /agents/a2a/register` (change 8), storing the card, roles and run.
- Tests for both state-mapping tables, JSON-RPC errors, version negotiation, caller scoping and card validity. A curl walk-through in the docs. An "A2A interop" section in the README.

**4b. Outbound bridge** (can follow 4a). `scripts/a2a_bridge.py` claims from `a2a.<agent>`, takes locks for declared paths, sends the work, maps state back, and turns `INPUT_REQUIRED` into decisions. It is tested against a stub A2A agent built with `a2a-sdk` in-process.

**Out of scope, as before:** a registry beyond card storage, durable storage, replacing auth, DNS and TLS, and renaming. Also out: streaming, push notifications, signed cards and the gRPC and REST bindings.

## 7. Decisions needed before Phase 4

| | Decision | Recommendation |
| --- | --- | --- |
| A | Blocking `SendMessage`: cap it at 30 s and return `WORKING`, or block until done? | Cap it, and document the deviation. Clients poll `GetTask`. |
| B | Implementation: hand-rolled JSON-RPC in Flask, or mount the Starlette app from `a2a-sdk` beside Flask? | Hand-rolled in Flask. Four methods, no new runtime dependency, one process (locks are in memory). Use `a2a-sdk` only in tests. |
| C | 4a only, or 4a and 4b together? | 4a first. 4b once a real external agent is ready to try. |
| D | Roles model: allowlists for `skills`, `queues`, `channels` and `lock_paths` per registered agent? | Yes, deny by default |
| E | Card fetching: https only, with a host allowlist? | https only, no private IPs, with `BUS_A2A_ALLOW_HOSTS` for dev |
| F | Serve the legacy `/.well-known/agent.json` alias? | Only if a client you care about needs it |
| G | Turn on `BUS_REQUIRE_RUN_TOKEN` wherever A2A is exposed? | Yes. External agents must never hold the shared token. |

## 8. As implemented

Decisions taken: A capped blocking (30 s, then the live state), B hand-rolled
JSON-RPC in Flask with `a2a-sdk` used only in tests, C both directions (4a
inbound and 4b bridge), D deny-by-default grants, E https-only card fetch with
no private addresses unless `BUS_A2A_ALLOW_HOSTS`, F no legacy `agent.json`
alias, G `/a2a` accepts run tokens only.

| Piece | Where |
| --- | --- |
| Protocol helpers, card, fetch guard, task table, client | `a2a_interop.py` (not `a2a.py`: that name would shadow the SDK package) |
| Card, `/a2a`, registration, grants, task hooks | `server.py` |
| Outbound bridge | `scripts/a2a_bridge.py` |
| Tests | `tests/test_a2a.py`, `tests/test_a2a_bridge.py`; wire JSON is parsed with the SDK's protobuf types |

Details that the design left open:

- **Task ownership** is the caller's agent handle, not its run, so an agent
  that re-registers keeps its tasks. Another caller gets `TaskNotFound`.
- **Selecting a skill** uses `metadata.skill` on the request or message
  (default `enqueue-work`), with `metadata.queue` and `metadata.lockPaths`.
  A restricted agent with no explicit queue uses the first non-wildcard queue
  in its grants.
- **Results** come from the worker: `ack` takes an optional `result` and
  `nack` an optional `reason`, and an orchestration `step.status` message may
  carry `result` or `reason`. The MCP adapter's `ack_work` and `nack_work`
  pass them through.
- **Questions** link to a task through the work item: the adapter adds
  `headers.work_message_id` to `request_decision` when the session holds one
  claimed item. A `provide_decision` posted on the bus by anyone also returns
  the task to working.
- **Permission errors** use JSON-RPC code -32000 (implementation-defined
  range) with `google.rpc.ErrorInfo` reason `PERMISSION_DENIED` and domain
  `iac-bus`; A2A defines no permission code.
- **Grants** are enforced on `/bus/*` as well as `/a2a`: channels for
  messages, queues for claim, ack and nack, lock paths for acquire. Restricted
  runs cannot post `orchestration.*` messages or list runs and agents. The
  operator can also restrict any run with `grants` on `/agents/register`.
- **Storage**: A2A task records, like locks and runs, live in memory and are
  written through to SQLite with `IAC_BUS_STORE=sqlite`, so tasks, run tokens
  and registrations (card and grants) survive a restart. Orchestration job
  state does not, so `run-job` tasks stop following their step after one.
- **Bridge limits**: the bridge renews the item's lease while the remote task
  runs. If a question gets no decision within `decision_wait`, the bridge
  cancels the remote task and fails the item.
- **Cancel** removes a work item nobody has claimed, marks a job step
  skipped, and posts `cancel_task` on `a2a.task.<id>` for a worker that has
  already claimed it.

