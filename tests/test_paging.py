"""GET /bus/messages cursor rules, on both stores.

A reader that pages with since_id must see every retained message exactly
once and in order, however far behind it fell; a cursor the bus no longer
holds must be reported, not silently replayed or skipped. Before 0.3.0 the
since_id read returned the NEWEST `limit` messages after the cursor, so a
reader more than one page behind silently lost the head of the backlog.
"""

import importlib
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _unload_server():
    mod = sys.modules.pop("server", None)
    if mod is not None and hasattr(mod, "_store"):
        mod._store.close()


@pytest.fixture(params=["memory", "sqlite"])
def client(request, monkeypatch, tmp_path):
    monkeypatch.setenv("BUS_API_TOKEN", "")
    monkeypatch.setenv("IAC_BUS_STORE", request.param)
    if request.param == "sqlite":
        monkeypatch.setenv("IAC_BUS_DB", str(tmp_path / "bus.db"))
    else:
        monkeypatch.delenv("IAC_BUS_DB", raising=False)
    _unload_server()
    import server  # noqa: F401

    srv = importlib.reload(sys.modules["server"])
    if request.param == "memory":
        srv._bus_messages.clear()
    yield srv.app.test_client()
    _unload_server()


def _post(client, channel, text):
    resp = client.post("/bus/messages", json={"channel": channel, "message": text})
    assert resp.status_code == 201, resp.get_json()
    return resp.get_json()["message"]["id"]


def _get(client, **params):
    query = "&".join(f"{k}={v}" for k, v in params.items())
    resp = client.get(f"/bus/messages?{query}")
    assert resp.status_code == 200, resp.get_json()
    return resp.get_json()


def test_a_reader_far_behind_pages_forward_without_loss(client):
    first = _post(client, "c", "m0")
    for i in range(1, 12):
        _post(client, "c", f"m{i}")
    seen, cursor, pages = [], first, 0
    while True:
        body = _get(client, channel="c", since_id=cursor, limit=4)
        assert body["cursor_lost"] is False
        pages += 1
        seen.extend(m["message"] for m in body["messages"])
        if not body["messages"]:
            break
        cursor = body["messages"][-1]["id"]
        if not body["has_more"]:
            break
    assert seen == [f"m{i}" for i in range(1, 12)], "every message after the cursor, once, in order"
    assert pages == 3


def test_has_more_is_false_on_the_last_page(client):
    first = _post(client, "c", "a")
    _post(client, "c", "b")
    body = _get(client, channel="c", since_id=first, limit=4)
    assert [m["message"] for m in body["messages"]] == ["b"]
    assert body["has_more"] is False


def test_an_unknown_cursor_is_reported_with_the_oldest_page(client):
    for text in ["a", "b", "c"]:
        _post(client, "c", text)
    body = _get(client, channel="c", since_id="no-such-id", limit=2)
    assert body["cursor_lost"] is True
    assert [m["message"] for m in body["messages"]] == ["a", "b"]
    assert body["has_more"] is True


def test_a_cursor_from_another_channel_counts_as_lost(client):
    other = _post(client, "x", "elsewhere")
    _post(client, "c", "here")
    body = _get(client, channel="c", since_id=other)
    assert body["cursor_lost"] is True
    assert [m["message"] for m in body["messages"]] == ["here"]


def test_without_a_cursor_the_newest_page_is_unchanged(client):
    for i in range(5):
        _post(client, "c", f"m{i}")
    body = _get(client, channel="c", limit=2)
    assert [m["message"] for m in body["messages"]] == ["m3", "m4"]
    assert body["has_more"] is False
    assert "cursor_lost" not in body


def test_a_lost_cursor_answers_a_long_poll_at_once(client):
    _post(client, "c", "a")
    body = _get(client, channel="c", since_id="gone", wait_seconds=5)
    assert body["cursor_lost"] is True
    assert [m["message"] for m in body["messages"]] == ["a"]
