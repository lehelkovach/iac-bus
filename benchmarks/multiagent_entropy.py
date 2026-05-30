"""Synthetic benchmark for multi-agent coordination entropy.

The benchmark models a single umbrella project ("lipmess") under two modes:

1. without_bus: ad-hoc swarm coordination with stale local state and collisions
2. with_bus: centralized task coordination similar to queue leasing/orchestration

It is intentionally deterministic for a given seed so tests can assert
relative behavior between modes.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, dataclass
from random import Random
from statistics import mean
from typing import Any


@dataclass(frozen=True)
class TaskSpec:
    task_id: str
    effort: int
    depends_on: tuple[str, ...] = ()


@dataclass(frozen=True)
class SimulationMetrics:
    mode: str
    seed: int
    tasks_total: int
    tasks_completed: int
    completion_ratio: float
    ticks_elapsed: int
    productive_units: int
    duplicate_units: int
    rework_units: int
    blocked_agent_ticks: int
    collision_events: int
    coordination_messages: int
    entropy_score: float
    productivity_score: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


LIPMESS_TASKS: tuple[TaskSpec, ...] = (
    TaskSpec("requirements", effort=2),
    TaskSpec("api-contract", effort=3, depends_on=("requirements",)),
    TaskSpec("data-model", effort=3, depends_on=("requirements",)),
    TaskSpec("backend-core", effort=5, depends_on=("api-contract", "data-model")),
    TaskSpec("frontend-core", effort=4, depends_on=("api-contract",)),
    TaskSpec("coordination-hooks", effort=4, depends_on=("backend-core",)),
    TaskSpec("tests-unit", effort=3, depends_on=("backend-core", "frontend-core")),
    TaskSpec("tests-integration", effort=4, depends_on=("tests-unit", "coordination-hooks")),
    TaskSpec("docs-handoff", effort=2, depends_on=("api-contract",)),
    TaskSpec("release", effort=2, depends_on=("tests-integration", "docs-handoff")),
)


def _entropy_score(
    duplicate_units: int,
    rework_units: int,
    blocked_agent_ticks: int,
    collision_events: int,
) -> float:
    return round(
        (duplicate_units * 1.7)
        + (rework_units * 2.4)
        + (blocked_agent_ticks * 0.35)
        + (collision_events * 1.1),
        3,
    )


def _productivity_score(tasks_completed: int, ticks_elapsed: int, productive_units: int) -> float:
    if ticks_elapsed <= 0:
        return 0.0
    return round((tasks_completed / ticks_elapsed) + (productive_units / (ticks_elapsed * 10.0)), 3)


def _summarize(metrics: list[SimulationMetrics]) -> dict[str, float]:
    return {
        "completion_ratio": round(mean(m.completion_ratio for m in metrics), 4),
        "ticks_elapsed": round(mean(m.ticks_elapsed for m in metrics), 2),
        "entropy_score": round(mean(m.entropy_score for m in metrics), 3),
        "productivity_score": round(mean(m.productivity_score for m in metrics), 3),
        "duplicate_units": round(mean(m.duplicate_units for m in metrics), 2),
        "rework_units": round(mean(m.rework_units for m in metrics), 2),
        "collision_events": round(mean(m.collision_events for m in metrics), 2),
        "blocked_agent_ticks": round(mean(m.blocked_agent_ticks for m in metrics), 2),
        "coordination_messages": round(mean(m.coordination_messages for m in metrics), 2),
    }


def _run_without_bus(seed: int, max_ticks: int, agent_count: int) -> SimulationMetrics:
    rng = Random(seed)
    task_by_id = {task.task_id: task for task in LIPMESS_TASKS}
    done: set[str] = set()
    progress: dict[str, int] = defaultdict(int)
    local_done = [set() for _ in range(agent_count)]
    update_queues: list[list[tuple[int, str]]] = [[] for _ in range(agent_count)]

    productive_units = 0
    duplicate_units = 0
    rework_units = 0
    blocked_agent_ticks = 0
    collision_events = 0
    coordination_messages = 0
    ticks_elapsed = 0

    for tick in range(1, max_ticks + 1):
        ticks_elapsed = tick

        for idx, queue in enumerate(update_queues):
            due = [task_id for ready_tick, task_id in queue if ready_tick <= tick]
            if due:
                local_done[idx].update(due)
            update_queues[idx] = [
                (ready_tick, task_id)
                for ready_tick, task_id in queue
                if ready_tick > tick
            ]

        if len(done) == len(LIPMESS_TASKS):
            break

        attempts: dict[str, list[int]] = defaultdict(list)
        for agent_idx in range(agent_count):
            near_ready = [
                task.task_id
                for task in LIPMESS_TASKS
                if task.task_id not in local_done[agent_idx]
                and any(dep not in done for dep in task.depends_on)
            ]
            local_ready = [
                task.task_id
                for task in LIPMESS_TASKS
                if task.task_id not in local_done[agent_idx]
                and all(dep in local_done[agent_idx] for dep in task.depends_on)
            ]
            selected_task: str | None = None
            if local_ready:
                selected_task = sorted(local_ready)[0]
                if near_ready and rng.random() < 0.25:
                    selected_task = sorted(near_ready)[0]
                elif len(local_ready) > 1 and rng.random() < 0.2:
                    selected_task = rng.choice(local_ready)
            else:
                if near_ready and rng.random() < 0.55:
                    selected_task = sorted(near_ready)[0]

            if selected_task is None:
                blocked_agent_ticks += 1
                continue

            attempts[selected_task].append(agent_idx)
            # Ad-hoc chatter: many messages, low coordination quality.
            coordination_messages += 1

        for task_id, assigned_agents in attempts.items():
            task = task_by_id[task_id]
            deps_done = all(dep in done for dep in task.depends_on)
            if not deps_done:
                rework_units += len(assigned_agents)
                blocked_agent_ticks += len(assigned_agents)
                continue

            if len(assigned_agents) > 1:
                collision_events += 1
                duplicate_units += len(assigned_agents) - 1

            productive_units += 1
            progress[task_id] += 1

            if progress[task_id] >= task.effort and task_id not in done:
                done.add(task_id)
                for worker_idx in range(agent_count):
                    propagation_delay = rng.randint(1, 4)
                    update_queues[worker_idx].append((tick + propagation_delay, task_id))

        if len(done) == len(LIPMESS_TASKS):
            break

    completion_ratio = round(len(done) / len(LIPMESS_TASKS), 4)
    entropy_score = _entropy_score(
        duplicate_units=duplicate_units,
        rework_units=rework_units,
        blocked_agent_ticks=blocked_agent_ticks,
        collision_events=collision_events,
    )
    productivity_score = _productivity_score(
        tasks_completed=len(done),
        ticks_elapsed=ticks_elapsed,
        productive_units=productive_units,
    )
    return SimulationMetrics(
        mode="without_bus",
        seed=seed,
        tasks_total=len(LIPMESS_TASKS),
        tasks_completed=len(done),
        completion_ratio=completion_ratio,
        ticks_elapsed=ticks_elapsed,
        productive_units=productive_units,
        duplicate_units=duplicate_units,
        rework_units=rework_units,
        blocked_agent_ticks=blocked_agent_ticks,
        collision_events=collision_events,
        coordination_messages=coordination_messages,
        entropy_score=entropy_score,
        productivity_score=productivity_score,
    )


def _run_with_bus(seed: int, max_ticks: int, agent_count: int) -> SimulationMetrics:
    task_by_id = {task.task_id: task for task in LIPMESS_TASKS}
    done: set[str] = set()
    in_progress: set[str] = set()
    progress: dict[str, int] = defaultdict(int)
    assignment: dict[int, str | None] = {idx: None for idx in range(agent_count)}

    productive_units = 0
    duplicate_units = 0
    rework_units = 0
    blocked_agent_ticks = 0
    collision_events = 0
    coordination_messages = 0
    ticks_elapsed = 0

    for tick in range(1, max_ticks + 1):
        ticks_elapsed = tick
        if len(done) == len(LIPMESS_TASKS):
            break

        ready_tasks = [
            task.task_id
            for task in LIPMESS_TASKS
            if task.task_id not in done
            and task.task_id not in in_progress
            and all(dep in done for dep in task.depends_on)
        ]

        for agent_idx in range(agent_count):
            if assignment[agent_idx] is not None:
                continue
            if ready_tasks:
                task_id = ready_tasks.pop(0)
                assignment[agent_idx] = task_id
                in_progress.add(task_id)
                coordination_messages += 1  # assignment message
            else:
                blocked_agent_ticks += 1

        for agent_idx, task_id in assignment.items():
            if task_id is None:
                continue

            task = task_by_id[task_id]
            productive_units += 1
            progress[task_id] += 1
            coordination_messages += 1  # progress heartbeat

            if progress[task_id] >= task.effort:
                done.add(task_id)
                in_progress.discard(task_id)
                assignment[agent_idx] = None
                coordination_messages += 1  # done/handoff message
            else:
                # keep the same task assigned until complete
                assignment[agent_idx] = task.task_id

        if len(done) == len(LIPMESS_TASKS):
            break

    completion_ratio = round(len(done) / len(LIPMESS_TASKS), 4)
    entropy_score = _entropy_score(
        duplicate_units=duplicate_units,
        rework_units=rework_units,
        blocked_agent_ticks=blocked_agent_ticks,
        collision_events=collision_events,
    )
    productivity_score = _productivity_score(
        tasks_completed=len(done),
        ticks_elapsed=ticks_elapsed,
        productive_units=productive_units,
    )
    return SimulationMetrics(
        mode="with_bus",
        seed=seed,
        tasks_total=len(LIPMESS_TASKS),
        tasks_completed=len(done),
        completion_ratio=completion_ratio,
        ticks_elapsed=ticks_elapsed,
        productive_units=productive_units,
        duplicate_units=duplicate_units,
        rework_units=rework_units,
        blocked_agent_ticks=blocked_agent_ticks,
        collision_events=collision_events,
        coordination_messages=coordination_messages,
        entropy_score=entropy_score,
        productivity_score=productivity_score,
    )


def run_lipmess_trial(mode: str, seed: int, max_ticks: int = 120, agent_count: int = 5) -> SimulationMetrics:
    if mode == "without_bus":
        return _run_without_bus(seed=seed, max_ticks=max_ticks, agent_count=agent_count)
    if mode == "with_bus":
        return _run_with_bus(seed=seed, max_ticks=max_ticks, agent_count=agent_count)
    raise ValueError(f"Unknown mode '{mode}'. Expected 'without_bus' or 'with_bus'.")


def run_lipmess_comparison(
    seeds: list[int] | tuple[int, ...] = (3, 7, 11, 19, 23, 29, 31, 37),
    max_ticks: int = 24,
    agent_count: int = 5,
) -> dict[str, Any]:
    without_bus = [
        run_lipmess_trial("without_bus", seed=seed, max_ticks=max_ticks, agent_count=agent_count)
        for seed in seeds
    ]
    with_bus = [
        run_lipmess_trial("with_bus", seed=seed, max_ticks=max_ticks, agent_count=agent_count)
        for seed in seeds
    ]

    without_summary = _summarize(without_bus)
    with_summary = _summarize(with_bus)

    delta = {
        "completion_ratio_gain": round(
            with_summary["completion_ratio"] - without_summary["completion_ratio"], 4
        ),
        "entropy_drop": round(
            without_summary["entropy_score"] - with_summary["entropy_score"], 3
        ),
        "productivity_gain": round(
            with_summary["productivity_score"] - without_summary["productivity_score"], 3
        ),
    }

    return {
        "scenario": "lipmess-umbrella-project",
        "seeds": list(seeds),
        "without_bus": without_summary,
        "with_bus": with_summary,
        "delta": delta,
        "trials": {
            "without_bus": [trial.to_dict() for trial in without_bus],
            "with_bus": [trial.to_dict() for trial in with_bus],
        },
    }

