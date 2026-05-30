#!/usr/bin/env python3
"""Run the lipmess multi-agent coordination benchmark."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from benchmarks.multiagent_entropy import run_lipmess_comparison


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Compare multi-agent task execution entropy with/without "
            "IAC-bus-style coordination."
        )
    )
    parser.add_argument(
        "--seeds",
        default="3,7,11,19,23,29,31,37",
        help="Comma-separated random seeds for repeated trials.",
    )
    parser.add_argument(
        "--max-ticks",
        type=int,
        default=24,
        help="Maximum ticks per trial before timeout.",
    )
    parser.add_argument(
        "--agents",
        type=int,
        default=5,
        help="Number of simulated worker agents.",
    )
    parser.add_argument(
        "--pretty",
        action="store_true",
        help="Print a compact human-readable summary in addition to JSON.",
    )
    return parser.parse_args()


def _format_pretty(report: dict) -> str:
    no_bus = report["without_bus"]
    with_bus = report["with_bus"]
    delta = report["delta"]
    return "\n".join(
        [
            "LIPMESS benchmark results",
            "-------------------------",
            f"Completion ratio: {no_bus['completion_ratio']} -> {with_bus['completion_ratio']}",
            f"Entropy score:    {no_bus['entropy_score']} -> {with_bus['entropy_score']}",
            f"Productivity:     {no_bus['productivity_score']} -> {with_bus['productivity_score']}",
            f"Entropy drop:     {delta['entropy_drop']}",
            f"Completion gain:  {delta['completion_ratio_gain']}",
        ]
    )


def main() -> int:
    args = _parse_args()
    seeds = [int(value.strip()) for value in args.seeds.split(",") if value.strip()]
    report = run_lipmess_comparison(
        seeds=seeds,
        max_ticks=args.max_ticks,
        agent_count=args.agents,
    )
    if args.pretty:
        print(_format_pretty(report))
        print()
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

