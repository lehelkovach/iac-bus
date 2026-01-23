import pytest

from agent_contract import (
    AgentAction,
    AgentKind,
    AgentMessage,
    AgentPolicy,
    AgentLease,
    new_message,
)


def test_agent_message_roundtrip():
    policy = AgentPolicy(
        allowlist=["read", "write"],
        max_runtime_seconds=120,
        workspace="/workspace",
    )
    lease = AgentLease(
        queue="work",
        message_id="msg-1",
        lease_id="lease-1",
        lease_until=123.4,
    )
    msg = new_message(
        kind=AgentKind.COMMAND,
        action=AgentAction.START_TASK,
        source="supervisor-1",
        target="agent-1",
        payload={"task": "analyze repo"},
        policy=policy,
        lease=lease,
        conversation_id="conv-1",
        trace_id="trace-1",
        metadata={"priority": 1},
    )

    data = msg.to_dict()
    assert data["kind"] == "command"
    assert data["action"] == "start_task"
    assert data["from"] == "supervisor-1"
    assert data["to"] == "agent-1"
    assert data["policy"]["max_runtime_seconds"] == 120

    parsed = AgentMessage.from_dict(data)
    assert parsed.kind == AgentKind.COMMAND
    assert parsed.action == AgentAction.START_TASK
    assert parsed.source == "supervisor-1"
    assert parsed.target == "agent-1"
    assert parsed.policy is not None
    assert parsed.policy.workspace == "/workspace"


def test_agent_message_missing_required():
    with pytest.raises(ValueError):
        AgentMessage.from_dict({"action": "start_task", "from": "agent-1"})
    with pytest.raises(ValueError):
        AgentMessage.from_dict({"kind": "command", "from": "agent-1"})
    with pytest.raises(ValueError):
        AgentMessage.from_dict({"kind": "command", "action": "start_task"})


def test_agent_message_allows_custom_action():
    data = {
        "kind": "command",
        "action": "custom_action",
        "from": "agent-1",
    }
    parsed = AgentMessage.from_dict(data)
    assert parsed.action == "custom_action"
