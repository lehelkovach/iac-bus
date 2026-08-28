"""
Tests for the swarm harness itself.

The harness gates deployments, so it has to be right in both directions: green
against a healthy bus, and loud when coordination actually breaks. A detector
that silently stops detecting is worse than no detector.
"""

from __future__ import annotations

import importlib.util
import os
import sys

import pytest

pytestmark = pytest.mark.swarm

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _load_harness():
    path = os.path.join(ROOT, "scripts", "swarm_harness.py")
    spec = importlib.util.spec_from_file_location("swarm_harness", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["swarm_harness"] = module
    spec.loader.exec_module(module)
    return module


harness = _load_harness()


def _args(scenario: str, bus, **overrides):
    argv = [scenario, "--bus-url", bus.url, "--token", bus.token]
    for key, value in overrides.items():
        argv.extend([f"--{key.replace('_', '-')}", str(value)])
    return harness.build_parser().parse_args(argv)


def test_flood_scenario_is_clean_on_a_healthy_bus(bus):
    args = _args("flood", bus, workers=6, tasks=120, timeout=60)
    result = harness.scenario_flood(args, bus.client)

    assert result.violations == [], [v.to_dict() for v in result.violations]
    assert result.metrics["tasks_completed"] == 120
    assert result.metrics["tasks_lost"] == 0
    assert result.metrics["tasks_duplicated"] == 0


def test_flood_scenario_detects_lost_work(bus_factory):
    """Starve the retention buffer and the harness must report the loss."""
    bus = bus_factory(BUS_MAX_MESSAGES=40)
    args = _args("flood", bus, workers=2, tasks=200, timeout=60)
    result = harness.scenario_flood(args, bus.client)

    codes = {v.code for v in result.violations}
    assert "task_loss" in codes, "harness failed to notice that tasks were dropped"
    assert result.metrics["tasks_lost"] > 0
    assert not result.ok


def test_dag_scenario_completes_and_respects_ordering(bus):
    args = _args("dag", bus, workers=4, width=5, work_ms=10, timeout=60)
    result = harness.scenario_dag(args, bus.client)

    assert result.violations == [], [v.to_dict() for v in result.violations]
    assert result.metrics["completed_steps"] == result.metrics["steps"]
    assert result.metrics["observed_parallelism"] > 1, (
        "the fan-out steps never actually ran at the same time"
    )


def test_fanout_scenario_is_clean_on_a_healthy_bus(bus):
    args = _args("fanout", bus, subscribers=3, messages=30, settle_seconds=1.0, timeout=60)
    result = harness.scenario_fanout(args, bus.client)

    assert result.violations == [], [v.to_dict() for v in result.violations]
    assert set(result.metrics["observed_per_subscriber"].values()) == {30}


def test_fanout_scenario_detects_dropped_broadcasts(bus_factory):
    """Retention pressure loses coordination messages; the harness must say so."""
    bus = bus_factory(BUS_MAX_MESSAGES=40)
    args = _args(
        "fanout", bus, subscribers=2, messages=40, pressure=30, settle_seconds=1.0, timeout=60
    )
    result = harness.scenario_fanout(args, bus.client)

    codes = {v.code for v in result.violations}
    assert codes & {"missed_messages", "duplicate_observation"}, (
        "harness saw a clean run while the bus was dropping broadcast messages"
    )


def test_embedded_mode_runs_without_a_deployment():
    """`--embedded` has to work, since that is how CI runs the harness."""
    exit_code = harness.main(
        [
            "flood",
            "--embedded",
            "--workers",
            "4",
            "--tasks",
            "40",
            "--timeout",
            "60",
        ]
    )
    assert exit_code == 0
