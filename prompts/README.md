# Agent Prompt Scaffolding

This directory defines reusable prompt files for ACP-managed agents.

## Identity Convention
Use readable handles mapped to immutable UUIDs.

- Handle format: `agent:<brand>.<repo>.<index>@<medium>`
- Example: `agent:cursor.iac-bus.0@web`
- Canonical identity in storage: `agent_uuid` (never changes)

## Prompt Files
- `orchestrator.prompt.md` - parent/supervisor agent behavior
- `worker.prompt.md` - execution-focused subordinate behavior
- `reviewer.prompt.md` - verification and quality gate behavior
- `REPO_AGENT_TAKEOVER.prompt.md` - **first-read takeover prompt for any agent continuing repo work**

## Mandatory takeover sequence
Any new agent taking over work should read in this order:

1. `prompts/REPO_AGENT_TAKEOVER.prompt.md`
2. review recent PR/branch history (as required in takeover prompt)
3. `docs/AGENT_TASKS.md`
4. then continue implementation from the top pending task

## Required Prompt Header Fields
Every prompt file should define:

- `AgentHandle`
- `AgentUUID`
- `Role`
- `RepoScope`
- `AuthorityLevel`
- `InputChannels`
- `OutputChannels`
- `SuccessCriteria`

## Required Behavioral Blocks
Every prompt should include:

1. Goal
2. Inputs
3. Context
4. Tools
5. Constraints
6. OutputFormat
7. CompletionChecks

## Coordination Rules
- Read latest coordination events before planning.
- Respect task dependency states and lock ownership.
- Emit provenance-rich events for every material action.
- Escalate blockers instead of silently bypassing constraints.
