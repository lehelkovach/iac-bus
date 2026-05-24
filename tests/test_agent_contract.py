import pytest

from agent_contract import (
    AgentAction,
    AgentKind,
    AgentMessage,
    AgentPolicy,
    AgentLease,
    ChoiceOption,
    DecisionRequest,
    DecisionResponse,
    InputRequest,
    InputResponse,
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


def test_input_request_roundtrip():
    request = InputRequest(
        prompt="Need approval",
        choices=[
            ChoiceOption(option_id="continue", label="Continue"),
            ChoiceOption(option_id="pause", label="Pause and review"),
        ],
        allow_freeform=False,
        requires_approval=True,
        escalation_chain=["supervisor-1", "user"],
    )
    data = request.to_dict()
    assert data["prompt"] == "Need approval"
    assert data["choices"][0]["id"] == "continue"

    parsed = InputRequest.from_dict(data)
    assert parsed.prompt == "Need approval"
    assert parsed.choices
    assert parsed.choices[0].option_id == "continue"


def test_input_response_roundtrip():
    response = InputResponse(
        response_text="Approved",
        approved=True,
        notes="Proceed",
    )
    data = response.to_dict()
    assert data["response_text"] == "Approved"
    parsed = InputResponse.from_dict(data)
    assert parsed.response_text == "Approved"
    assert parsed.approved is True


def test_decision_request_roundtrip():
    request = DecisionRequest(
        prompt="Select next step",
        choices=[
            ChoiceOption(option_id="continue", label="Continue"),
            ChoiceOption(option_id="pause", label="Pause"),
        ],
        default_choice="continue",
    )
    data = request.to_dict()
    assert data["default_choice"] == "continue"
    parsed = DecisionRequest.from_dict(data)
    assert parsed.prompt == "Select next step"
    assert parsed.choices[1].option_id == "pause"


def test_decision_response_roundtrip():
    response = DecisionResponse(
        choice_id="continue",
        approved=True,
        notes="Proceed",
    )
    data = response.to_dict()
    assert data["choice_id"] == "continue"
    parsed = DecisionResponse.from_dict(data)
    assert parsed.choice_id == "continue"
    assert parsed.approved is True


def test_agent_message_payload_dataclass():
    request = InputRequest(prompt="Proceed?")
    msg = new_message(
        kind=AgentKind.CONTROL,
        action=AgentAction.REQUEST_INPUT,
        source="agent-1",
        payload=request,
    )
    data = msg.to_dict()
    assert data["payload"]["prompt"] == "Proceed?"
