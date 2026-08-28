"""
Fixtures that run the bus as a real HTTP server.

Swarm behaviour depends on concurrent socket traffic, lease timing, and the
retention buffer, none of which are exercised faithfully by Flask's in-process
test client. These fixtures start `server.py` on an ephemeral port so tests hit
the same code path a deployed agent would.
"""

from __future__ import annotations

import importlib
import os
import sys
import threading
from dataclasses import dataclass
from typing import Any, Dict, Optional

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from bus_client import BusClient  # noqa: E402

DEFAULT_ENV = {
    "BUS_API_TOKEN": "",
    "BUS_MAX_MESSAGES": "5000",
    "BUS_RETENTION_SECONDS": "3600",
    "BUS_QUEUE_LEASE_SECONDS": "60",
    "BUS_LOG_LEVEL": "ERROR",
}


@dataclass
class BusProcess:
    """A running bus plus the knobs tests need to talk to it."""

    url: str
    token: str
    _http: Any
    _thread: threading.Thread

    def client(self, timeout: float = 10.0) -> BusClient:
        """A fresh client; give every worker thread its own."""
        return BusClient(self.url, token=self.token, timeout=timeout)

    def stop(self) -> None:
        self._http.shutdown()
        self._thread.join(timeout=5)


def _start_bus(env: Optional[Dict[str, str]] = None) -> BusProcess:
    from werkzeug.serving import make_server

    settings = dict(DEFAULT_ENV)
    settings.update({k: str(v) for k, v in (env or {}).items()})
    for key, value in settings.items():
        os.environ[key] = value

    # server.py reads its configuration at import time, so a reload is how a
    # test applies new settings.
    if "server" in sys.modules:
        bus_server = importlib.reload(sys.modules["server"])
    else:
        import server as bus_server

    bus_server._bus_messages.clear()
    bus_server._jobs.clear()

    http = make_server("127.0.0.1", 0, bus_server.app, threaded=True)
    thread = threading.Thread(target=http.serve_forever, daemon=True)
    thread.start()
    return BusProcess(
        url=f"http://127.0.0.1:{http.server_port}",
        token=settings["BUS_API_TOKEN"],
        _http=http,
        _thread=thread,
    )


@pytest.fixture
def bus_factory():
    """Start one or more buses with custom settings; all are stopped on teardown."""
    started = []

    def factory(**env) -> BusProcess:
        # Only one bus may run at a time: reloading the module rebinds the
        # shared in-memory state that any earlier instance is still serving.
        for bus in started:
            bus.stop()
        started.clear()
        bus = _start_bus(env)
        started.append(bus)
        return bus

    yield factory

    for bus in started:
        bus.stop()


@pytest.fixture
def bus(bus_factory) -> BusProcess:
    """A bus with generous limits, for tests that are not about resource pressure."""
    return bus_factory()
