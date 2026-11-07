"""Locks, runs and A2A tasks survive a bus restart with the SQLite store."""

import importlib
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import store as store_mod  # noqa: E402

SHARED = {"Authorization": "Bearer shared-secret"}


def _store(path):
    return store_mod.SqliteStore(str(path))


def test_locks_survive_restart_and_tokens_keep_rising(tmp_path):
    db = tmp_path / "bus.db"
    s1 = _store(db)
    lock, _, _ = s1.locks.acquire("r", "src", "agent-a", "exclusive", 600)
    shared, _, _ = s1.locks.acquire("r", "docs", "agent-b", "shared", 600)
    gone, _, _ = s1.locks.acquire("r", "tmp", "agent-c", "exclusive", 600)
    s1.locks.release(gone["lock_id"], "agent-c")
    s1.close()

    s2 = _store(db)
    assert s2.locks.durable is True
    held = {l["resource"]: l for l in s2.locks.list()}
    assert set(held) == {"src", "docs"}
    blocked, conflicts, _ = s2.locks.acquire("r", "src/app.py", "agent-z", "exclusive", 60)
    assert blocked is None and conflicts[0]["agent"] == "agent-a"
    ok, reason, _ = s2.locks.validate("r", "src/app.py", lock["fencing_token"])
    assert ok, reason
    renewed, error = s2.locks.renew(lock["lock_id"], "agent-a", 900)
    assert error is None and renewed["fencing_token"] == lock["fencing_token"]
    after, _, _ = s2.locks.acquire("r", "tmp", "agent-c", "exclusive", 60)
    assert after["fencing_token"] > max(lock["fencing_token"], shared["fencing_token"], gone["fencing_token"])
    s2.close()


def test_lock_that_expired_while_down_is_gone(tmp_path):
    db = tmp_path / "bus.db"
    s1 = _store(db)
    s1.locks.acquire("r", "src", "agent-a", "exclusive", 0.2)
    s1.close()
    time.sleep(0.3)
    s2 = _store(db)
    assert s2.locks.list() == []
    s2.close()
    s3 = _store(db)
    assert s3.journal.load("lock") == []
    s3.close()


def test_runs_survive_restart(tmp_path):
    db = tmp_path / "bus.db"
    s1 = _store(db)
    run, token = s1.runs.create("claude.a", "u1", 3600)
    ended, ended_token = s1.runs.create("claude.b", "u2", 3600)
    s1.runs.end(ended["run_id"])
    s1.close()

    s2 = _store(db)
    again, error = s2.runs.authenticate(token)
    assert error is None and again["actor"] == run["actor"]
    assert s2.runs.authenticate(ended_token) == (None, "run ended")
    assert "token_hash" not in json.dumps(s2.runs.list(active_only=False))
    s2.close()


def test_old_finished_records_are_pruned(tmp_path, monkeypatch):
    db = tmp_path / "bus.db"
    s1 = _store(db)
    old_run, _ = s1.runs.create("claude.old", "u", 60)
    s1.runs.end(old_run["run_id"])
    s1.a2a_tasks.create({"id": "t-old", "owner": "x", "state": "TASK_STATE_COMPLETED", "status_ts": 0})
    s1.a2a_tasks.create({"id": "t-live", "owner": "x", "state": "TASK_STATE_WORKING", "status_ts": 0})
    s1._conn.execute("UPDATE durable_records SET updated_at = 0")
    s1._conn.commit()
    s1.close()
    s2 = _store(db)
    assert s2.runs.get(old_run["run_id"]) is None
    assert s2.a2a_tasks.get("t-old") is None and s2.a2a_tasks.get("t-live") is not None
    s2.close()


def test_memory_store_is_not_durable():
    s = store_mod.MemoryStore()
    assert not s.locks.durable and not s.runs.durable and not s.a2a_tasks.durable


def _server(monkeypatch, db):
    monkeypatch.setenv("BUS_API_TOKEN", "shared-secret")
    monkeypatch.setenv("IAC_BUS_STORE", "sqlite")
    monkeypatch.setenv("IAC_BUS_DB", str(db))
    if "server" in sys.modules:
        del sys.modules["server"]
    import server  # noqa: F401
    return importlib.reload(sys.modules["server"])


def test_bus_restart_keeps_identity_locks_and_a2a_tasks(monkeypatch, tmp_path):
    db = tmp_path / "bus.db"
    srv = _server(monkeypatch, db)
    c = srv.app.test_client()
    reg = c.post("/agents/register", json={"handle": "claude.a"}, headers=SHARED).get_json()
    auth = {"Authorization": "Bearer " + reg["run_token"]}
    lock = c.post("/bus/locks/acquire", json={"resource": "src/app.py", "agent": "x"}, headers=auth).get_json()
    a2a_headers = dict(auth, **{"A2A-Version": "1.0"})
    sent = c.post("/a2a", headers=a2a_headers, json={"jsonrpc": "2.0", "id": 1, "method": "SendMessage", "params": {
        "message": {"messageId": "m", "role": "ROLE_USER", "parts": [{"text": "do it"}]},
        "configuration": {"returnImmediately": True}, "metadata": {"queue": "q"}}}).get_json()
    task_id = sent["result"]["task"]["id"]
    srv._store.close()

    srv2 = _server(monkeypatch, db)
    c2 = srv2.app.test_client()
    assert c2.get("/agents/whoami", headers=auth).get_json()["actor"] == reg["actor"]
    held = c2.get("/bus/locks", headers=auth).get_json()["locks"]
    assert [(l["resource"], l["agent"]) for l in held] == [("src/app.py", reg["actor"])]
    assert c2.post("/bus/locks/release", json={"lock_id": lock["lock_id"], "agent": "x"}, headers=auth).status_code == 200
    got = c2.post("/a2a", headers=a2a_headers, json={"jsonrpc": "2.0", "id": 2, "method": "GetTask",
                                                    "params": {"id": task_id}}).get_json()
    assert got["result"]["status"]["state"] == "TASK_STATE_SUBMITTED"
    assert c2.get("/health").get_json()["locks_durable"] is True
    srv2._store.close()
