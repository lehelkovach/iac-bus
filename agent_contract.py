#!/usr/bin/env python3
"""
Universal agent contract for behavior, control, and message marshalling.
"""

from __future__ import annotations

import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Iterable, Mapping, Optional, Union


class AgentKind(str, Enum):
    COMMAND = "command"
    EVENT = "event"
    STATUS = "status"
    RESPONSE = "response"
    CONTROL = "control"
    HEARTBEAT = "heartbeat"
    REGISTRATION = "registration"


class AgentState(str, Enum):
    IDLE = "idle"
    RUNNING = "running"
    WAITING_INPUT = "waiting_input"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"


class AgentAction(str, Enum):
    REGISTER = "register"
    HEARTBEAT = "heartbeat"
    START_TASK = "start_task"
    PAUSE_TASK = "pause_task"
    RESUME_TASK = "resume_task"
    CANCEL_TASK = "cancel_task"
    REQUEST_INPUT = "request_input"
    PROVIDE_INPUT = "provide_input"
    REQUEST_DECISION = "request_decision"
    PROVIDE_DECISION = "provide_decision"
    CLAIM_WORK = "claim_work"
    ACK_WORK = "ack_work"
    NACK_WORK = "nack_work"
    REPORT_STATUS = "report_status"
    REPORT_RESULT = "report_result"
    UPDATE_POLICY = "update_policy"


@dataclass
class AgentPolicy:
    allowlist: Optional[list[str]] = None
    denylist: Optional[list[str]] = None
    max_runtime_seconds: Optional[int] = None
    max_cost: Optional[float] = None
    max_tokens: Optional[int] = None
    tools: Optional[list[str]] = None
    workspace: Optional[str] = None
    env: Optional[Dict[str, Any]] = None
    secrets_ref: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        data = {
            "allowlist": self.allowlist,
            "denylist": self.denylist,
            "max_runtime_seconds": self.max_runtime_seconds,
            "max_cost": self.max_cost,
            "max_tokens": self.max_tokens,
            "tools": self.tools,
            "workspace": self.workspace,
            "env": self.env,
            "secrets_ref": self.secrets_ref,
        }
        return {key: value for key, value in data.items() if value is not None}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "AgentPolicy":
        return cls(
            allowlist=data.get("allowlist"),
            denylist=data.get("denylist"),
            max_runtime_seconds=data.get("max_runtime_seconds"),
            max_cost=data.get("max_cost"),
            max_tokens=data.get("max_tokens"),
            tools=data.get("tools"),
            workspace=data.get("workspace"),
            env=data.get("env"),
            secrets_ref=data.get("secrets_ref"),
        )


@dataclass
class AgentLease:
    queue: Optional[str] = None
    message_id: Optional[str] = None
    lease_id: Optional[str] = None
    lease_until: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        data = {
            "queue": self.queue,
            "message_id": self.message_id,
            "lease_id": self.lease_id,
            "lease_until": self.lease_until,
        }
        return {key: value for key, value in data.items() if value is not None}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "AgentLease":
        return cls(
            queue=data.get("queue"),
            message_id=data.get("message_id"),
            lease_id=data.get("lease_id"),
            lease_until=data.get("lease_until"),
        )


@dataclass
class ChoiceOption:
    option_id: str
    label: str
    description: Optional[str] = None
    value: Optional[Any] = None

    def to_dict(self) -> Dict[str, Any]:
        data = {
            "id": self.option_id,
            "label": self.label,
            "description": self.description,
            "value": self.value,
        }
        return {key: value for key, value in data.items() if value is not None}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ChoiceOption":
        return cls(
            option_id=data["id"],
            label=data["label"],
            description=data.get("description"),
            value=data.get("value"),
        )


@dataclass
class InputRequest:
    prompt: str
    choices: Optional[list[ChoiceOption]] = None
    allow_freeform: bool = True
    min_choices: Optional[int] = None
    max_choices: Optional[int] = None
    timeout_seconds: Optional[int] = None
    requires_approval: bool = False
    escalation_chain: Optional[list[str]] = None
    context: Optional[Dict[str, Any]] = None
    metadata: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        data = {
            "prompt": self.prompt,
            "choices": [choice.to_dict() for choice in self.choices] if self.choices else None,
            "allow_freeform": self.allow_freeform,
            "min_choices": self.min_choices,
            "max_choices": self.max_choices,
            "timeout_seconds": self.timeout_seconds,
            "requires_approval": self.requires_approval,
            "escalation_chain": self.escalation_chain,
            "context": self.context,
            "metadata": self.metadata,
        }
        return {key: value for key, value in data.items() if value is not None}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "InputRequest":
        return cls(
            prompt=data["prompt"],
            choices=[ChoiceOption.from_dict(option) for option in data.get("choices", [])] or None,
            allow_freeform=data.get("allow_freeform", True),
            min_choices=data.get("min_choices"),
            max_choices=data.get("max_choices"),
            timeout_seconds=data.get("timeout_seconds"),
            requires_approval=data.get("requires_approval", False),
            escalation_chain=data.get("escalation_chain"),
            context=data.get("context"),
            metadata=data.get("metadata"),
        )


@dataclass
class InputResponse:
    response_text: Optional[str] = None
    choice_ids: Optional[list[str]] = None
    approved: Optional[bool] = None
    notes: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        data = {
            "response_text": self.response_text,
            "choice_ids": self.choice_ids,
            "approved": self.approved,
            "notes": self.notes,
            "metadata": self.metadata,
        }
        return {key: value for key, value in data.items() if value is not None}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "InputResponse":
        return cls(
            response_text=data.get("response_text"),
            choice_ids=data.get("choice_ids"),
            approved=data.get("approved"),
            notes=data.get("notes"),
            metadata=data.get("metadata"),
        )


@dataclass
class DecisionRequest:
    prompt: str
    choices: list[ChoiceOption]
    default_choice: Optional[str] = None
    timeout_seconds: Optional[int] = None
    requires_approval: bool = True
    escalation_chain: Optional[list[str]] = None
    context: Optional[Dict[str, Any]] = None
    metadata: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        data = {
            "prompt": self.prompt,
            "choices": [choice.to_dict() for choice in self.choices],
            "default_choice": self.default_choice,
            "timeout_seconds": self.timeout_seconds,
            "requires_approval": self.requires_approval,
            "escalation_chain": self.escalation_chain,
            "context": self.context,
            "metadata": self.metadata,
        }
        return {key: value for key, value in data.items() if value is not None}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "DecisionRequest":
        return cls(
            prompt=data["prompt"],
            choices=[ChoiceOption.from_dict(option) for option in data.get("choices", [])],
            default_choice=data.get("default_choice"),
            timeout_seconds=data.get("timeout_seconds"),
            requires_approval=data.get("requires_approval", True),
            escalation_chain=data.get("escalation_chain"),
            context=data.get("context"),
            metadata=data.get("metadata"),
        )


@dataclass
class DecisionResponse:
    choice_id: Optional[str] = None
    decision: Optional[str] = None
    approved: Optional[bool] = None
    notes: Optional[str] = None
    rationale: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        data = {
            "choice_id": self.choice_id,
            "decision": self.decision,
            "approved": self.approved,
            "notes": self.notes,
            "rationale": self.rationale,
            "metadata": self.metadata,
        }
        return {key: value for key, value in data.items() if value is not None}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "DecisionResponse":
        return cls(
            choice_id=data.get("choice_id"),
            decision=data.get("decision"),
            approved=data.get("approved"),
            notes=data.get("notes"),
            rationale=data.get("rationale"),
            metadata=data.get("metadata"),
        )


@dataclass
class ProgressUpdate:
    summary: Optional[str] = None
    progress_pct: Optional[float] = None
    state: Optional[StateType] = None
    details: Optional[Dict[str, Any]] = None
    metadata: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        data = {
            "summary": self.summary,
            "progress_pct": self.progress_pct,
            "state": AgentMessage._enum_value(self.state),
            "details": self.details,
            "metadata": self.metadata,
        }
        return {key: value for key, value in data.items() if value is not None}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ProgressUpdate":
        return cls(
            summary=data.get("summary"),
            progress_pct=data.get("progress_pct"),
            state=_coerce_enum(data.get("state"), AgentState),
            details=data.get("details"),
            metadata=data.get("metadata"),
        )


ActionType = Union[AgentAction, str]
KindType = Union[AgentKind, str]
StateType = Union[AgentState, str]


@dataclass
class AgentMessage:
    kind: KindType
    action: ActionType
    source: str
    target: Optional[str] = None
    message_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    timestamp: float = field(default_factory=time.time)
    protocol: str = "iac-agent/1.0"
    conversation_id: Optional[str] = None
    trace_id: Optional[str] = None
    reply_to: Optional[str] = None
    state: Optional[StateType] = None
    payload: Optional[Any] = None
    policy: Optional[AgentPolicy] = None
    lease: Optional[AgentLease] = None
    metadata: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        data: Dict[str, Any] = {
            "id": self.message_id,
            "ts": self.timestamp,
            "protocol": self.protocol,
            "kind": self._enum_value(self.kind),
            "action": self._enum_value(self.action),
            "from": self.source,
            "to": self.target,
            "conversation_id": self.conversation_id,
            "trace_id": self.trace_id,
            "reply_to": self.reply_to,
            "state": self._enum_value(self.state),
            "payload": _payload_to_dict(self.payload),
            "policy": self.policy.to_dict() if self.policy else None,
            "lease": self.lease.to_dict() if self.lease else None,
            "metadata": self.metadata,
        }
        return {key: value for key, value in data.items() if value is not None}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "AgentMessage":
        if "kind" not in data:
            raise ValueError("kind is required")
        if "action" not in data:
            raise ValueError("action is required")
        if "from" not in data:
            raise ValueError("from is required")
        return cls(
            kind=_coerce_enum(data["kind"], AgentKind),
            action=_coerce_enum(data["action"], AgentAction),
            source=data["from"],
            target=data.get("to"),
            message_id=data.get("id", uuid.uuid4().hex),
            timestamp=data.get("ts", time.time()),
            protocol=data.get("protocol", "iac-agent/1.0"),
            conversation_id=data.get("conversation_id"),
            trace_id=data.get("trace_id"),
            reply_to=data.get("reply_to"),
            state=_coerce_enum(data.get("state"), AgentState),
            payload=data.get("payload"),
            policy=AgentPolicy.from_dict(data["policy"]) if data.get("policy") else None,
            lease=AgentLease.from_dict(data["lease"]) if data.get("lease") else None,
            metadata=data.get("metadata"),
        )

    @staticmethod
    def _enum_value(value: Optional[Union[Enum, str]]) -> Optional[str]:
        if value is None:
            return None
        if isinstance(value, Enum):
            return value.value
        return str(value)


def _coerce_enum(value: Any, enum_type: type[Enum]) -> Any:
    if value is None:
        return None
    try:
        return enum_type(value)
    except ValueError:
        return value


def _payload_to_dict(payload: Any) -> Any:
    if payload is None:
        return None
    to_dict = getattr(payload, "to_dict", None)
    if callable(to_dict):
        return to_dict()
    return payload


def parse_payload(action: ActionType, payload: Any) -> Any:
    if payload is None:
        return None
    action_value = action.value if isinstance(action, Enum) else action
    if not isinstance(payload, Mapping):
        return payload
    if action_value == AgentAction.REQUEST_INPUT.value:
        return InputRequest.from_dict(payload)
    if action_value == AgentAction.PROVIDE_INPUT.value:
        return InputResponse.from_dict(payload)
    if action_value == AgentAction.REQUEST_DECISION.value:
        return DecisionRequest.from_dict(payload)
    if action_value == AgentAction.PROVIDE_DECISION.value:
        return DecisionResponse.from_dict(payload)
    if action_value == AgentAction.REPORT_STATUS.value:
        return ProgressUpdate.from_dict(payload)
    return payload


def new_message(
    *,
    kind: KindType,
    action: ActionType,
    source: str,
    target: Optional[str] = None,
    payload: Optional[Any] = None,
    policy: Optional[AgentPolicy] = None,
    lease: Optional[AgentLease] = None,
    conversation_id: Optional[str] = None,
    trace_id: Optional[str] = None,
    reply_to: Optional[str] = None,
    state: Optional[StateType] = None,
    metadata: Optional[Dict[str, Any]] = None,
    protocol: str = "iac-agent/1.0",
) -> AgentMessage:
    return AgentMessage(
        kind=kind,
        action=action,
        source=source,
        target=target,
        payload=payload,
        policy=policy,
        lease=lease,
        conversation_id=conversation_id,
        trace_id=trace_id,
        reply_to=reply_to,
        state=state,
        metadata=metadata,
        protocol=protocol,
    )


def request_input_message(
    *,
    source: str,
    target: Optional[str],
    request: InputRequest,
    kind: KindType = AgentKind.CONTROL,
    **kwargs: Any,
) -> AgentMessage:
    return new_message(
        kind=kind,
        action=AgentAction.REQUEST_INPUT,
        source=source,
        target=target,
        payload=request,
        **kwargs,
    )


def provide_input_message(
    *,
    source: str,
    target: Optional[str],
    response: InputResponse,
    kind: KindType = AgentKind.RESPONSE,
    **kwargs: Any,
) -> AgentMessage:
    return new_message(
        kind=kind,
        action=AgentAction.PROVIDE_INPUT,
        source=source,
        target=target,
        payload=response,
        **kwargs,
    )


def request_decision_message(
    *,
    source: str,
    target: Optional[str],
    request: DecisionRequest,
    kind: KindType = AgentKind.CONTROL,
    **kwargs: Any,
) -> AgentMessage:
    return new_message(
        kind=kind,
        action=AgentAction.REQUEST_DECISION,
        source=source,
        target=target,
        payload=request,
        **kwargs,
    )


def provide_decision_message(
    *,
    source: str,
    target: Optional[str],
    response: DecisionResponse,
    kind: KindType = AgentKind.RESPONSE,
    **kwargs: Any,
) -> AgentMessage:
    return new_message(
        kind=kind,
        action=AgentAction.PROVIDE_DECISION,
        source=source,
        target=target,
        payload=response,
        **kwargs,
    )


def status_update_message(
    *,
    source: str,
    target: Optional[str],
    update: ProgressUpdate,
    kind: KindType = AgentKind.STATUS,
    **kwargs: Any,
) -> AgentMessage:
    return new_message(
        kind=kind,
        action=AgentAction.REPORT_STATUS,
        source=source,
        target=target,
        payload=update,
        **kwargs,
    )


class AgentAdapter(ABC):
    """
    Adapter interface to marshal/unmarshal messages to a provider API.
    """

    @property
    @abstractmethod
    def provider(self) -> str:
        raise NotImplementedError

    @abstractmethod
    def marshal(self, message: AgentMessage) -> Dict[str, Any]:
        raise NotImplementedError

    @abstractmethod
    def unmarshal(self, payload: Mapping[str, Any]) -> AgentMessage:
        raise NotImplementedError

    @abstractmethod
    def send(self, message: AgentMessage) -> None:
        raise NotImplementedError

    @abstractmethod
    def poll(self) -> Iterable[AgentMessage]:
        raise NotImplementedError
