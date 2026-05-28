AgentHandle: agent:cursor.iac-bus.<ordinal>@<medium>
AgentUUID: <uuid>
Role: master-or-worker (as assigned)
RepoScope: iac-bus
AuthorityLevel: role-scoped
InputChannels: bus-events, task-updates, human-commands
OutputChannels: progress-events, blocker-events, handoff-events
SuccessCriteria: unblock deployment path and continue implementation backlog safely

## Goal
Take over work on `lehelkovach/iac-bus` without losing context, and continue
execution from the latest documented state.

## First-Read Files (in order)
1. `README.md`
2. `docs/ACP_PROTOCOL_V2.md`
3. `docs/FULL_DEV_PLAN.md`
4. `docs/TESTING_STRATEGY.md`
5. `docs/AGENT_TASKS.md` (canonical active task queue)

## Required Branch/PR Review (before execution)
Before writing code or running deployments, review what was already completed in
recent branches and pull requests.

Minimum review checklist:
1. list open and recent merged PRs
2. inspect changed files and PR descriptions for active branch context
3. list local and remote branches
4. confirm whether current branch is ahead/behind master
5. update `docs/AGENT_TASKS.md` if discovered work changes priority/order

Suggested commands:
- `gh pr list --state open --limit 20`
- `gh pr list --state merged --limit 20`
- `gh pr view <number> --json title,body,files,headRefName,baseRefName`
- `git branch -a`
- `git status --short --branch`
- `git log --oneline --decorate -n 20`

## Required Secret Check (before execution)
Check whether all required deployment/provisioning secrets are injected in the
current runtime environment.

For OCI provisioning:
- `OCI_TENANCY_OCID`
- `OCI_USER_OCID`
- `OCI_FINGERPRINT`
- `OCI_REGION`
- `OCI_COMPARTMENT_OCID`
- `OCI_SUBNET_OCID`
- `OCI_IMAGE_OCID`
- `OCI_SSH_PUBLIC_KEY`
- one of: `OCI_PRIVATE_KEY` or `OCI_PRIVATE_KEY_B64`

For existing dev VM deploy path:
- `KSG_DEV_VM_HOST`
- `KSG_DEV_VM_USER`
- `KSG_DEV_VM_PORT`
- `KSG_DEV_VM_APP_DIR`
- `KSG_DEV_VM_KEY`

Never print secret values. Log only presence/absence.

## Constraint Rules
- If required secrets are missing, do not fake success.
- Record blocker clearly in `docs/AGENT_TASKS.md` and stop at safe boundary.
- Keep IAC-Bus generic (no KSG/OpenClaw business logic in bus core).
- Run tests before/after significant changes.
- Preserve existing branch changes unless explicitly asked to revert.

## Output Format
- Branch and commit
- Actions taken
- Tests run + results
- Blockers (if any)
- Exact next command(s) to resume

## Completion Checks
- Latest tasks in `docs/AGENT_TASKS.md` are synchronized with actual repo state.
- Deployment/provisioning status is explicitly confirmed or blocked with evidence.
- No undocumented assumptions remain for the next takeover agent.
