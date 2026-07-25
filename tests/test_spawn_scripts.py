"""Unit tests for Cursor spawn/terminate scripts (mocked HTTP, no live API key)."""

import importlib.util
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"


def _load_module(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


spawn_mod = _load_module("spawn_cursor_agents", "spawn-cursor-agents.py")
terminate_mod = _load_module("handoff_terminate_agents", "handoff-terminate-agents.py")


class TestSpawnCursorAgents:
    def test_cursor_headers_use_bearer(self):
        headers = spawn_mod._cursor_headers("test-key")
        assert headers["Authorization"] == "Bearer test-key"
        assert headers["Content-Type"] == "application/json"

    def test_spawn_agents_sends_payload_without_name_injection(self):
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {"id": "agent-uuid-1"}

        payload = {"prompt": {"text": "Do the task"}}

        with patch.object(spawn_mod.requests, "post", return_value=mock_resp) as post:
            agents = spawn_mod.spawn_agents(
                "https://api.cursor.com/v0/agents",
                "key-abc",
                payload,
                count=2,
            )

        assert len(agents) == 2
        assert post.call_count == 2

        for call in post.call_args_list:
            assert call.kwargs["headers"]["Authorization"] == "Bearer key-abc"
            body = call.kwargs["json"]
            assert "name" not in body
            assert body == payload

    def test_announce_agents_posts_to_bus(self):
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()

        agents = [{"id": "550e8400-e29b-41d4-a716-446655440000"}]

        with patch.object(spawn_mod.requests, "post", return_value=mock_resp) as post:
            spawn_mod.announce_agents(
                "http://127.0.0.1:8091",
                "bus-token",
                agents,
                "ops",
            )

        post.assert_called_once()
        url = post.call_args.args[0]
        assert url == "http://127.0.0.1:8091/bus/messages"
        assert post.call_args.kwargs["headers"]["Authorization"] == "Bearer bus-token"
        body = post.call_args.kwargs["json"]
        assert body["channel"] == "ops"
        assert body["message"] == "spawned:550e8400-e29b-41d4-a716-446655440000"
        assert body["kind"] == "lifecycle.spawned"


class TestHandoffTerminateAgents:
    def test_cursor_headers_use_bearer(self):
        headers = terminate_mod._cursor_headers("test-key")
        assert headers["Authorization"] == "Bearer test-key"

    def test_terminate_agent_sends_bearer_delete(self):
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        agent_id = "550e8400-e29b-41d4-a716-446655440000"

        with patch.object(terminate_mod.requests, "delete", return_value=mock_resp) as delete:
            terminate_mod._terminate_agent(
                "https://api.cursor.com/v0/agents",
                "key-xyz",
                agent_id,
            )

        delete.assert_called_once_with(
            f"https://api.cursor.com/v0/agents/{agent_id}",
            headers={"Authorization": "Bearer key-xyz"},
            timeout=30,
        )

    def test_parse_ids_extracts_uuids(self):
        text = "spawned:550e8400-e29b-41d4-a716-446655440000 and other"
        ids = terminate_mod._parse_ids(text)
        assert ids == ["550e8400-e29b-41d4-a716-446655440000"]
