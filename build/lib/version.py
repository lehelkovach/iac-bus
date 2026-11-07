"""Single source of version for health/metrics and release tagging.

From a checkout the version is the VERSION file; when installed as a package
it comes from the package metadata (pyproject.toml reads the same file).
"""

from __future__ import annotations

from pathlib import Path

_ROOT = Path(__file__).resolve().parent


def get_version() -> str:
    path = _ROOT / "VERSION"
    try:
        text = path.read_text(encoding="utf-8").strip()
        if text:
            return text
    except OSError:
        pass
    try:
        from importlib.metadata import version

        return version("iac-bus")
    except Exception:  # noqa: BLE001 - not installed as a package either
        return "0.0.0"


__version__ = get_version()
