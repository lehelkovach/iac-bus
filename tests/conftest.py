"""Shared pytest helpers for IAC Bus tests."""

import importlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def load_server(monkeypatch, token=""):
    """Reload server with an isolated in-memory SQLite store."""
    monkeypatch.setenv("BUS_API_TOKEN", token)
    monkeypatch.setenv("BUS_DB_PATH", ":memory:")
    # Drop cached modules so env + store re-init cleanly.
    for name in ("server", "store"):
        if name in sys.modules:
            del sys.modules[name]
    import server  # noqa: F401

    server = importlib.reload(sys.modules["server"])
    server.reset_store(":memory:")
    server._jobs.clear()
    return server
