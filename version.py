"""Single source of version for health/metrics and release tagging."""

from __future__ import annotations

from pathlib import Path

_ROOT = Path(__file__).resolve().parent


def get_version() -> str:
    path = _ROOT / "VERSION"
    try:
        return path.read_text(encoding="utf-8").strip() or "0.0.0"
    except OSError:
        return "0.0.0"


__version__ = get_version()
