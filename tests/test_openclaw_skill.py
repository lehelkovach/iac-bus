import json
from urllib.parse import parse_qs, urlparse

from skills.openclaw.iac_bus import IacBusClient


def test_skill_sends_bearer_token_and_lists_messages():
    calls = []

    def transport(method, url, headers, body, timeout):
        calls.append((method, url, headers, body, timeout))
        return 200, "application/json", b'{"messages":[]}'

    bus = IacBusClient("http://bus.example", token="secret", transport=transport)
    result = bus.list_messages(channel="ops", since_id="m1", limit=25, include_queue=True)

    assert result == {"messages": []}
    method, url, headers, body, timeout = calls[0]
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    assert method == "GET"
    assert parsed.path == "/bus/messages"
    assert query == {
        "channel": ["ops"],
        "since_id": ["m1"],
        "limit": ["25"],
        "include_queue": ["true"],
    }
    assert headers["Authorization"] == "Bearer secret"
    assert body is None
    assert timeout == 30


def test_skill_session_progress_helper_posts_convention_message():
    calls = []

    def transport(method, url, headers, body, timeout):
        calls.append((method, url, headers, body, timeout))
        return 201, "application/json", b'{"success":true}'

    bus = IacBusClient("http://bus.example", transport=transport)
    result = bus.session_progress(
        "session-1",
        "agent.cursor.demo",
        "Halfway done",
        metadata={"percent": 50},
    )

    assert result == {"success": True}
    method, url, headers, body, _timeout = calls[0]
    payload = json.loads(body.decode("utf-8"))
    assert method == "POST"
    assert url == "http://bus.example/bus/messages"
    assert "Authorization" not in headers
    assert payload == {
        "protocol": "iac-bus/1.0",
        "channel": "session.session-1",
        "sender": "agent.cursor.demo",
        "message": {
            "text": "Halfway done",
            "session_id": "session-1",
            "metadata": {"percent": 50},
        },
        "type": "progress",
        "conversation_id": "session-1",
        "queue": "",
        "priority": 0,
    }


def test_skill_claim_returns_none_on_empty_queue():
    def transport(method, url, headers, body, timeout):
        return 204, "", b""

    bus = IacBusClient("http://bus.example", transport=transport)
    assert bus.claim_queue("work", "agent-a") is None
