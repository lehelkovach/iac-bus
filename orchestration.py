#!/usr/bin/env python3
"""
Dependency-aware orchestration helpers for multi-agent work.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Iterable, Mapping, Optional


class StepStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"
    TIMED_OUT = "timed_out"

    @property
    def terminal(self) -> bool:
        return self in {StepStatus.COMPLETED, StepStatus.FAILED, StepStatus.SKIPPED, StepStatus.TIMED_OUT}


class BarrierMode(str, Enum):
    ALL_COMPLETED = "all_completed"
    ALL_TERMINAL = "all_terminal"


@dataclass(frozen=True)
class Dependency:
    step_id: str
    min_delay_seconds: Optional[int] = None

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Dependency":
        return cls(
            step_id=data["step_id"],
            min_delay_seconds=data.get("min_delay_seconds"),
        )

    def to_dict(self) -> Dict[str, Any]:
        data = {"step_id": self.step_id}
        if self.min_delay_seconds is not None:
            data["min_delay_seconds"] = self.min_delay_seconds
        return data


@dataclass(frozen=True)
class BarrierSpec:
    barrier_id: str
    requires: list[str]
    mode: BarrierMode = BarrierMode.ALL_COMPLETED

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "BarrierSpec":
        mode = data.get("mode", BarrierMode.ALL_COMPLETED.value)
        return cls(
            barrier_id=data["id"],
            requires=list(data.get("requires", [])),
            mode=BarrierMode(mode),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.barrier_id,
            "requires": list(self.requires),
            "mode": self.mode.value,
        }


@dataclass(frozen=True)
class StepSpec:
    step_id: str
    title: Optional[str] = None
    description: Optional[str] = None
    depends_on: list[Dependency] = field(default_factory=list)
    wait_for: list[str] = field(default_factory=list)
    not_before_ts: Optional[float] = None
    deadline_ts: Optional[float] = None
    parallel_group: Optional[str] = None
    queue: Optional[str] = None
    priority: int = 0
    metadata: Optional[Dict[str, Any]] = None

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "StepSpec":
        dependencies = []
        for item in data.get("depends_on", []):
            if isinstance(item, str):
                dependencies.append(Dependency(step_id=item))
            else:
                dependencies.append(Dependency.from_dict(item))
        return cls(
            step_id=data["id"],
            title=data.get("title"),
            description=data.get("description"),
            depends_on=dependencies,
            wait_for=list(data.get("wait_for", [])),
            not_before_ts=data.get("not_before_ts"),
            deadline_ts=data.get("deadline_ts"),
            parallel_group=data.get("parallel_group"),
            queue=data.get("queue"),
            priority=int(data.get("priority", 0)),
            metadata=data.get("metadata"),
        )

    def to_dict(self) -> Dict[str, Any]:
        data: Dict[str, Any] = {
            "id": self.step_id,
            "title": self.title,
            "description": self.description,
            "depends_on": [dep.to_dict() for dep in self.depends_on] if self.depends_on else None,
            "wait_for": list(self.wait_for) if self.wait_for else None,
            "not_before_ts": self.not_before_ts,
            "deadline_ts": self.deadline_ts,
            "parallel_group": self.parallel_group,
            "queue": self.queue,
            "priority": self.priority if self.priority != 0 else None,
            "metadata": self.metadata,
        }
        return {key: value for key, value in data.items() if value is not None}


@dataclass(frozen=True)
class JobSpec:
    job_id: str
    name: Optional[str]
    steps: list[StepSpec]
    barriers: list[BarrierSpec] = field(default_factory=list)
    description: Optional[str] = None
    created_at: Optional[float] = None
    deadline_ts: Optional[float] = None
    metadata: Optional[Dict[str, Any]] = None

    def step_map(self) -> Dict[str, StepSpec]:
        return {step.step_id: step for step in self.steps}

    def barrier_map(self) -> Dict[str, BarrierSpec]:
        return {barrier.barrier_id: barrier for barrier in self.barriers}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "JobSpec":
        steps = [StepSpec.from_dict(item) for item in data.get("steps", [])]
        barriers = [BarrierSpec.from_dict(item) for item in data.get("barriers", [])]
        return cls(
            job_id=data["job_id"],
            name=data.get("name"),
            description=data.get("description"),
            created_at=data.get("created_at"),
            deadline_ts=data.get("deadline_ts"),
            steps=steps,
            barriers=barriers,
            metadata=data.get("metadata"),
        )

    def to_dict(self) -> Dict[str, Any]:
        data: Dict[str, Any] = {
            "job_id": self.job_id,
            "name": self.name,
            "description": self.description,
            "created_at": self.created_at,
            "deadline_ts": self.deadline_ts,
            "steps": [step.to_dict() for step in self.steps],
            "barriers": [barrier.to_dict() for barrier in self.barriers] if self.barriers else None,
            "metadata": self.metadata,
        }
        return {key: value for key, value in data.items() if value is not None}


@dataclass
class StepState:
    status: StepStatus = StepStatus.PENDING
    assigned_to: Optional[str] = None
    started_at: Optional[float] = None
    completed_at: Optional[float] = None
    last_updated: Optional[float] = None
    attempts: int = 0

    def mark(self, status: StepStatus, now: Optional[float] = None) -> None:
        now = _now(now)
        self.status = status
        self.last_updated = now
        if status == StepStatus.RUNNING:
            self.started_at = now
            self.attempts += 1
        if status.terminal:
            self.completed_at = now

    def to_dict(self) -> Dict[str, Any]:
        data: Dict[str, Any] = {
            "status": self.status.value,
            "assigned_to": self.assigned_to,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "last_updated": self.last_updated,
            "attempts": self.attempts,
        }
        return {key: value for key, value in data.items() if value is not None}


@dataclass
class JobState:
    job_id: str
    step_states: Dict[str, StepState]

    def get_state(self, step_id: str) -> StepState:
        return self.step_states[step_id]


def _now(value: Optional[float] = None) -> float:
    return value if value is not None else time.time()


def validate_job_spec(job: JobSpec) -> None:
    step_ids = [step.step_id for step in job.steps]
    if len(step_ids) != len(set(step_ids)):
        raise ValueError("duplicate step ids in job spec")
    known = set(step_ids)
    for step in job.steps:
        for dep in step.depends_on:
            if dep.step_id not in known:
                raise ValueError(f"dependency {dep.step_id} not found in job {job.job_id}")
    for barrier in job.barriers:
        for step_id in barrier.requires:
            if step_id not in known:
                raise ValueError(f"barrier {barrier.barrier_id} references unknown step {step_id}")


def build_job_state(job: JobSpec, now: Optional[float] = None) -> JobState:
    now = _now(now)
    state = {step.step_id: StepState(last_updated=now) for step in job.steps}
    return JobState(job_id=job.job_id, step_states=state)


def update_timeouts(job: JobSpec, state: JobState, now: Optional[float] = None) -> list[str]:
    now = _now(now)
    timed_out = []
    for step in job.steps:
        if step.deadline_ts is None:
            continue
        step_state = state.get_state(step.step_id)
        if step_state.status != StepStatus.PENDING:
            continue
        if now > step.deadline_ts:
            step_state.mark(StepStatus.TIMED_OUT, now)
            timed_out.append(step.step_id)
    return timed_out


def _barrier_open(barrier: BarrierSpec, state: JobState) -> bool:
    statuses = [state.get_state(step_id).status for step_id in barrier.requires]
    if barrier.mode == BarrierMode.ALL_TERMINAL:
        return all(status.terminal for status in statuses)
    return all(status == StepStatus.COMPLETED for status in statuses)


def _dependencies_satisfied(step: StepSpec, state: JobState, now: float) -> bool:
    for dep in step.depends_on:
        dep_state = state.get_state(dep.step_id)
        if dep_state.status != StepStatus.COMPLETED:
            return False
        if dep.min_delay_seconds is not None:
            if dep_state.completed_at is None:
                return False
            if now < dep_state.completed_at + dep.min_delay_seconds:
                return False
    return True


def get_ready_steps(job: JobSpec, state: JobState, now: Optional[float] = None) -> list[StepSpec]:
    now = _now(now)
    update_timeouts(job, state, now)
    barriers = job.barrier_map()
    ready = []
    for step in job.steps:
        step_state = state.get_state(step.step_id)
        if step_state.status != StepStatus.PENDING:
            continue
        if step.not_before_ts is not None and now < step.not_before_ts:
            continue
        if step.deadline_ts is not None and now > step.deadline_ts:
            continue
        if not _dependencies_satisfied(step, state, now):
            continue
        barrier_blocked = False
        for barrier_id in step.wait_for:
            barrier = barriers.get(barrier_id)
            if not barrier:
                continue
            if not _barrier_open(barrier, state):
                barrier_blocked = True
                break
        if barrier_blocked:
            continue
        ready.append(step)
    ready.sort(key=lambda item: (-item.priority, item.step_id))
    return ready


def mark_step_running(state: JobState, step_id: str, now: Optional[float] = None, assigned_to: Optional[str] = None) -> None:
    step_state = state.get_state(step_id)
    step_state.assigned_to = assigned_to
    step_state.mark(StepStatus.RUNNING, now)


def mark_step_completed(state: JobState, step_id: str, now: Optional[float] = None) -> None:
    state.get_state(step_id).mark(StepStatus.COMPLETED, now)


def mark_step_failed(state: JobState, step_id: str, now: Optional[float] = None) -> None:
    state.get_state(step_id).mark(StepStatus.FAILED, now)


def mark_step_skipped(state: JobState, step_id: str, now: Optional[float] = None) -> None:
    state.get_state(step_id).mark(StepStatus.SKIPPED, now)


def job_is_complete(job: JobSpec, state: JobState) -> bool:
    return all(state.get_state(step.step_id).status.terminal for step in job.steps)


def iter_pending_steps(job: JobSpec, state: JobState) -> Iterable[StepSpec]:
    for step in job.steps:
        if state.get_state(step.step_id).status == StepStatus.PENDING:
            yield step
