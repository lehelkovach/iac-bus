import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from benchmarks.multiagent_entropy import run_lipmess_comparison, run_lipmess_trial


def test_lipmess_without_bus_exhibits_coordination_entropy():
    metrics = run_lipmess_trial("without_bus", seed=3, max_ticks=24, agent_count=5)

    assert metrics.tasks_completed < metrics.tasks_total
    assert metrics.duplicate_units > 0
    assert metrics.rework_units > 0
    assert metrics.collision_events > 0
    assert metrics.entropy_score > 100


def test_lipmess_with_bus_completes_timeboxed_workflow():
    metrics = run_lipmess_trial("with_bus", seed=3, max_ticks=24, agent_count=5)

    assert metrics.tasks_completed == metrics.tasks_total
    assert metrics.completion_ratio == 1.0
    assert metrics.duplicate_units == 0
    assert metrics.rework_units == 0
    assert metrics.collision_events == 0


def test_lipmess_comparison_shows_bus_improvements():
    report = run_lipmess_comparison(seeds=[3, 7, 11, 19], max_ticks=24, agent_count=5)

    assert report["delta"]["completion_ratio_gain"] >= 0.1
    assert report["delta"]["entropy_drop"] > 100
    assert report["delta"]["productivity_gain"] > 0
    assert (
        report["without_bus"]["coordination_messages"]
        > report["with_bus"]["coordination_messages"]
    )

