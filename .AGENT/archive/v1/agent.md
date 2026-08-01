# Agent Prompt — iac-bus

This is the live agent prompt for `iac-bus`. The **canonical, reusable base**
lives in `agent-repo-boilerplate/.AGENT/agent.md`; keep generic behavior there
and only repo-specific direction here.

> **Environment handoff (canonical):** this repo is one of the `knowshowgo +8` Cursor
> environment repos. Read `osl-oc-agent/.AGENT/handoffs/CURSOR-ENV-HANDOFF.md` first.

## Startup loop

At the start of each run, read `.AGENT/agent-run.md`, this file, and the latest
entries in `.AGENT/agent-action-log.md`. Check the current branch/working tree
before editing and preserve existing work.

## Repo role

Inter-Agent Communication Bus (IAC Bus): a lightweight Flask HTTP message bus for coordinating multiple agents (pub/sub channels + queue leasing). Also hosts OCI dev-VM provisioning + Cursor-agent spawn/terminate scripts under scripts/.

## Repo-specific direction

_(Add standing, repo-specific instructions here as they arise.)_
