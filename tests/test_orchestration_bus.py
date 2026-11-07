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


def test_orchestration_parallel_dispatch(monkeypatch):
    server = _load_server(monkeypatch, token="")
    server._bus_messages.clear()
    server._jobs.clear()
    with server._metrics_lock:
        server._metrics["orchestration_jobs"] = 0
        server._metrics["orchestration_dispatches"] = 0
    client = server.app.test_client()

    resp = client.post(
        "/bus/messages",
        json={
            "type": "orchestration.job",
            "message": {
                "job": {
                    "job_id": "job-parallel",
                    "steps": [
                        {"id": "a", "queue": "workers"},
                        {"id": "b", "queue": "workers"},
                        {
                            "id": "c",
                            "queue": "workers",
                            "depends_on": [
                                {"step_id": "a"},
                                {"step_id": "b"},
                            ],
                        },
                    ],
                }
            },
        },
    )
    assert resp.status_code == 201

    resp = client.get("/bus/messages?include_queue=true")
    messages = resp.get_json()["messages"]
    assert len(_collect_assignment(messages, "a")) == 1
    assert len(_collect_assignment(messages, "b")) == 1
    assert len(_collect_assignment(messages, "c")) == 0

    claim_a = client.post(
        "/bus/queues/claim",
        json={"queue": "workers", "worker": "w1", "lease_seconds": 30},
    )
    claim_b = client.post(
        "/bus/queues/claim",
        json={"queue": "workers", "worker": "w2", "lease_seconds": 30},
    )
    assert claim_a.status_code == 200
    assert claim_b.status_code == 200
    leased_ids = {
        claim_a.get_json()["message"]["message"]["step"]["id"],
        claim_b.get_json()["message"]["message"]["step"]["id"],
    }
    assert leased_ids == {"a", "b"}

    for step_id in ("a", "b"):
        status = client.post(
            "/bus/messages",
            json={
                "type": "orchestration.step.status",
                "sender": f"worker-{step_id}",
                "message": {
                    "job_id": "job-parallel",
                    "step_id": step_id,
                    "status": "completed",
                },
            },
        )
        assert status.status_code == 201

    resp = client.get("/bus/messages?include_queue=true")
    messages = resp.get_json()["messages"]
    assert len(_collect_assignment(messages, "c")) == 1

    metrics = client.get("/metrics").get_json()
    assert metrics["counters"]["orchestration_jobs"] >= 1
    assert metrics["counters"]["orchestration_dispatches"] >= 3
