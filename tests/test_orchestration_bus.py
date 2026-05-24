import importlib
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _load_server(monkeypatch, token=""):
    monkeypatch.setenv("BUS_API_TOKEN", token)
    if "server" in sys.modules:
        del sys.modules["server"]
    import server  # noqa: F401
    return importlib.reload(sys.modules["server"])


def _collect_assignment(messages, step_id):
    return [
        msg
        for msg in messages
        if msg.get("type") == "orchestration.step.assign"
        and msg.get("message", {}).get("step", {}).get("id") == step_id
    ]


def test_orchestration_job_dispatches_ready_steps(monkeypatch):
    server = _load_server(monkeypatch, token="")
    server._bus_messages.clear()
    server._jobs.clear()
    client = server.app.test_client()

    resp = client.post(
        "/bus/messages",
        json={
            "type": "orchestration.job",
            "message": {
                "job": {
                    "job_id": "job-1",
                    "name": "build-service",
                    "steps": [
                        {"id": "design"},
                        {"id": "impl", "depends_on": [{"step_id": "design"}]},
                    ],
                }
            },
        },
    )
    assert resp.status_code == 201

    resp = client.get("/bus/messages?include_queue=true")
    messages = resp.get_json()["messages"]
    assignments = _collect_assignment(messages, "design")
    assert len(assignments) == 1


def test_orchestration_step_status_enqueues_next(monkeypatch):
    server = _load_server(monkeypatch, token="")
    server._bus_messages.clear()
    server._jobs.clear()
    client = server.app.test_client()

    client.post(
        "/bus/messages",
        json={
            "type": "orchestration.job",
            "message": {
                "job": {
                    "job_id": "job-2",
                    "steps": [
                        {"id": "design"},
                        {"id": "impl", "depends_on": [{"step_id": "design"}]},
                    ],
                }
            },
        },
    )

    resp = client.post(
        "/bus/messages",
        json={
            "type": "orchestration.step.status",
            "sender": "agent-a",
            "message": {
                "job_id": "job-2",
                "step_id": "design",
                "status": "completed",
            },
        },
    )
    assert resp.status_code == 201

    resp = client.get("/bus/messages?include_queue=true")
    messages = resp.get_json()["messages"]
    assignments = _collect_assignment(messages, "impl")
    assert len(assignments) == 1
