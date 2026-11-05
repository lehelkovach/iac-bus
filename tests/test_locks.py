"""Path locks with fencing tokens: locks.py rules and the /bus/locks/* API."""

import importlib
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from locks import (  # noqa: E402
    MemoryLockTable,
    normalize_repo,
    normalize_resource,
    resource_covers,
    resources_overlap,
)


class FakeClock:
    def __init__(self, start=1_000_000.0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def _load_server(monkeypatch, token=""):
    monkeypatch.setenv("BUS_API_TOKEN", token)
    monkeypatch.delenv("IAC_BUS_STORE", raising=False)
    if "server" in sys.modules:
        del sys.modules["server"]
    import server  # noqa: F401
    return importlib.reload(sys.modules["server"])


@pytest.fixture
def server(monkeypatch):
    srv = _load_server(monkeypatch)
    srv._bus_messages.clear()
    return srv


@pytest.fixture
def client(server):
    return server.app.test_client()


def _acquire(client, resource, agent, **extra):
    body = {"resource": resource, "agent": agent, "ttl_seconds": 60}
    body.update(extra)
    return client.post("/bus/locks/acquire", json=body)


def _lock_events(client):
    resp = client.get("/bus/messages?channel=locks&limit=200")
    return resp.get_json()["messages"]


# ---------------------------------------------------------------- path rules


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("src/app.py", "src/app.py"),
        ("./src/app.py", "src/app.py"),
        ("src/auth/", "src/auth"),
        ("src//auth/./login.py", "src/auth/login.py"),
        ("src\\auth\\login.py", "src/auth/login.py"),
        ("  src/app.py  ", "src/app.py"),
        (".", "."),
        ("./", "."),
        ("src/../docs", "docs"),
    ],
)
def test_normalize_resource(raw, expected):
    assert normalize_resource(raw) == expected


@pytest.mark.parametrize(
    "raw", ["", "   ", "/etc/passwd", "C:/repo/x.py", "../other/x.py", "src/../../x", "src/*.py", 5, None]
)
def test_normalize_resource_rejects(raw):
    with pytest.raises(ValueError):
        normalize_resource(raw)


def test_normalize_repo():
    assert normalize_repo(None) == "default"
    assert normalize_repo("") == "default"
    assert normalize_repo("iac-bus") == "iac-bus"
    assert normalize_repo("lehelkovach/iac-bus") == "lehelkovach/iac-bus"
    with pytest.raises(ValueError):
        normalize_repo("a/b/c")
    with pytest.raises(ValueError):
        normalize_repo("bad repo")


def test_overlap_is_segment_based():
    assert resources_overlap("src/auth", "src/auth/login.py")
    assert resources_overlap("src/auth/login.py", "src/auth")
    assert resources_overlap("src/auth", "src/auth")
    assert not resources_overlap("src/auth", "src/authz.py")
    assert not resources_overlap("src/auth/a.py", "src/auth/b.py")
    assert resources_overlap(".", "anything/at/all.py")
    assert resource_covers("src", "src/app.py")
    assert not resource_covers("src/app.py", "src")


def test_table_fencing_tokens_strictly_increase():
    table = MemoryLockTable(first_token=1)
    tokens = []
    for i in range(5):
        lock, conflicts, _ = table.acquire("r", "src/app.py", f"agent-{i}", "exclusive", 30)
        assert lock and not conflicts
        tokens.append(lock["fencing_token"])
        table.release(lock["lock_id"], f"agent-{i}")
    assert tokens == sorted(tokens)
    assert len(set(tokens)) == len(tokens)


# ------------------------------------------------------------ HTTP: conflicts


def test_acquire_returns_lock_and_token(client):
    resp = _acquire(client, "src/app.py", "A", reason="refactor")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["lock_id"]
    assert isinstance(data["fencing_token"], int)
    assert data["expires_at"] > time.time()
    assert data["resource"] == "src/app.py"
    assert data["repo"] == "default"
    assert data["mode"] == "exclusive"
    assert data["agent"] == "A"


def test_exclusive_conflict_reports_holder(client):
    assert _acquire(client, "src/app.py", "A", reason="refactor").status_code == 200
    resp = _acquire(client, "./src/app.py", "B")
    assert resp.status_code == 409
    data = resp.get_json()
    assert data["held_by"] == "A"
    assert data["mode"] == "exclusive"
    assert data["reason"] == "refactor"
    assert data["expires_at"] > time.time()
    assert "lock_id" not in data
    assert all("lock_id" not in c for c in data["conflicts"])


def test_prefix_overlap_conflicts_both_ways(client):
    assert _acquire(client, "src/auth/", "A").status_code == 200
    assert _acquire(client, "src/auth/login.py", "B").status_code == 409
    assert _acquire(client, "src/authz.py", "B").status_code == 200
    assert _acquire(client, "docs/api.md", "C").status_code == 200
    assert _acquire(client, "docs", "B").status_code == 409
    assert _acquire(client, ".", "D").status_code == 409


def test_same_path_in_other_repo_does_not_conflict(client):
    assert _acquire(client, "src/app.py", "A", repo="iac-bus").status_code == 200
    assert _acquire(client, "src/app.py", "B", repo="knowshowgo").status_code == 200
    assert _acquire(client, "src/app.py", "B", repo="iac-bus").status_code == 409


def test_shared_vs_exclusive(client):
    assert _acquire(client, "src/app.py", "A", mode="shared").status_code == 200
    assert _acquire(client, "src/app.py", "B", mode="shared").status_code == 200
    resp = _acquire(client, "src/app.py", "C", mode="exclusive")
    assert resp.status_code == 409
    assert {c["agent"] for c in resp.get_json()["conflicts"]} == {"A", "B"}

    assert _acquire(client, "lib/x.py", "A", mode="exclusive").status_code == 200
    assert _acquire(client, "lib/x.py", "B", mode="shared").status_code == 409


def test_same_agent_can_nest_locks(client):
    assert _acquire(client, "src", "A").status_code == 200
    assert _acquire(client, "src/app.py", "A").status_code == 200


# --------------------------------------------------------- expiry and renew


def test_lock_expires_and_publishes_event(server, client):
    clock = FakeClock()
    server._store.locks._clock = clock
    assert _acquire(client, "src/app.py", "A", ttl_seconds=10).status_code == 200
    clock.advance(5)
    assert _acquire(client, "src/app.py", "B").status_code == 409
    clock.advance(6)
    assert _acquire(client, "src/app.py", "B").status_code == 200

    types = [m["type"] for m in _lock_events(client)]
    assert types == ["lock.acquired", "lock.expired", "lock.acquired"]


def test_expiry_event_appears_on_read_without_lock_calls(server, client):
    clock = FakeClock()
    server._store.locks._clock = clock
    assert _acquire(client, "src/app.py", "A", ttl_seconds=1).status_code == 200
    clock.advance(2)
    types = [m["type"] for m in _lock_events(client)]
    assert types == ["lock.acquired", "lock.expired"]
    assert client.get("/health").get_json()["locks_held"] == 0


def test_renew_extends_lease(server, client):
    clock = FakeClock()
    server._store.locks._clock = clock
    lock = _acquire(client, "src/app.py", "A", ttl_seconds=10).get_json()
    clock.advance(8)
    resp = client.post("/bus/locks/renew", json={"lock_id": lock["lock_id"], "agent": "A", "ttl_seconds": 10})
    assert resp.status_code == 200
    renewed = resp.get_json()
    assert renewed["expires_at"] == pytest.approx(clock.now + 10)
    assert renewed["fencing_token"] == lock["fencing_token"]
    clock.advance(8)
    assert _acquire(client, "src/app.py", "B").status_code == 409


def test_renew_after_expiry_is_404(server, client):
    clock = FakeClock()
    server._store.locks._clock = clock
    lock = _acquire(client, "src/app.py", "A", ttl_seconds=5).get_json()
    clock.advance(6)
    resp = client.post("/bus/locks/renew", json={"lock_id": lock["lock_id"], "agent": "A", "ttl_seconds": 5})
    assert resp.status_code == 404


def test_ttl_is_capped(server, client):
    data = _acquire(client, "src/app.py", "A", ttl_seconds=10**9).get_json()
    assert data["ttl_seconds"] == server.BUS_LOCK_TTL_MAX_SECONDS


# ----------------------------------------------------------------- ownership


def test_only_holder_can_renew_or_release(client):
    lock = _acquire(client, "src/app.py", "A").get_json()
    resp = client.post("/bus/locks/release", json={"lock_id": lock["lock_id"], "agent": "B"})
    assert resp.status_code == 409
    assert resp.get_json()["error"] == "lock owner mismatch"
    resp = client.post("/bus/locks/renew", json={"lock_id": lock["lock_id"], "agent": "B", "ttl_seconds": 5})
    assert resp.status_code == 409

    resp = client.post("/bus/locks/release", json={"lock_id": "nope", "agent": "A"})
    assert resp.status_code == 404

    resp = client.post("/bus/locks/release", json={"lock_id": lock["lock_id"], "agent": "A"})
    assert resp.status_code == 200
    assert "lock_id" not in resp.get_json()["lock"]
    resp = client.post("/bus/locks/release", json={"lock_id": lock["lock_id"], "agent": "A"})
    assert resp.status_code == 404


def test_release_lets_next_agent_in_and_publishes_events(client):
    lock = _acquire(client, "src/app.py", "A").get_json()
    client.post("/bus/locks/release", json={"lock_id": lock["lock_id"], "agent": "A"})
    assert _acquire(client, "src/app.py", "B").status_code == 200
    events = _lock_events(client)
    assert [m["type"] for m in events] == ["lock.acquired", "lock.released", "lock.acquired"]
    assert all(m["sender"] == "iac-bus" for m in events)
    assert all("lock_id" not in m["message"] for m in events)


# ---------------------------------------------------------------- fencing


def test_fencing_tokens_monotonic_over_http(client):
    tokens = []
    for agent in ("A", "B", "C"):
        lock = _acquire(client, "src/app.py", agent).get_json()
        tokens.append(lock["fencing_token"])
        client.post("/bus/locks/release", json={"lock_id": lock["lock_id"], "agent": agent})
    tokens.append(_acquire(client, "other.py", "D").get_json()["fencing_token"])
    assert all(b > a for a, b in zip(tokens, tokens[1:]))


def test_validate_rejects_stale_holder(server, client):
    clock = FakeClock()
    server._store.locks._clock = clock
    old = _acquire(client, "src", "A", ttl_seconds=10).get_json()

    ok = client.post("/bus/locks/validate", json={"resource": "src/app.py", "fencing_token": old["fencing_token"]})
    assert ok.status_code == 200
    assert ok.get_json()["valid"] is True

    outside = client.post("/bus/locks/validate", json={"resource": "docs/x.md", "fencing_token": old["fencing_token"]})
    assert outside.status_code == 409

    wrong_agent = client.post(
        "/bus/locks/validate",
        json={"resource": "src/app.py", "fencing_token": old["fencing_token"], "agent": "B"},
    )
    assert wrong_agent.status_code == 409

    clock.advance(11)
    new = _acquire(client, "src/app.py", "B").get_json()
    assert new["fencing_token"] > old["fencing_token"]
    stale = client.post("/bus/locks/validate", json={"resource": "src/app.py", "fencing_token": old["fencing_token"]})
    assert stale.status_code == 409
    assert stale.get_json()["valid"] is False
    fresh = client.post("/bus/locks/validate", json={"resource": "src/app.py", "fencing_token": new["fencing_token"]})
    assert fresh.status_code == 200


# ------------------------------------------------------------------- waiting


def test_wait_succeeds_when_holder_releases(server):
    holder = server.app.test_client()
    lock = _acquire(holder, "src/app.py", "A").get_json()
    result = {}

    def waiter():
        c = server.app.test_client()
        started = time.time()
        resp = _acquire(c, "src/app.py", "B", wait_seconds=5)
        result["status"] = resp.status_code
        result["elapsed"] = time.time() - started

    t = threading.Thread(target=waiter)
    t.start()
    time.sleep(0.3)
    holder.post("/bus/locks/release", json={"lock_id": lock["lock_id"], "agent": "A"})
    t.join(timeout=5)
    assert result["status"] == 200
    assert 0.2 <= result["elapsed"] < 3.0


def test_wait_succeeds_when_holder_lock_expires(server):
    client = server.app.test_client()
    assert _acquire(client, "src/app.py", "A", ttl_seconds=0.4).status_code == 200
    started = time.time()
    resp = _acquire(client, "src/app.py", "B", wait_seconds=5)
    assert resp.status_code == 200
    assert time.time() - started < 3.0


def test_wait_times_out_with_409(client):
    assert _acquire(client, "src/app.py", "A").status_code == 200
    started = time.time()
    resp = _acquire(client, "src/app.py", "B", wait_seconds=0.5)
    elapsed = time.time() - started
    assert resp.status_code == 409
    assert resp.get_json()["waited_seconds"] >= 0.45
    assert 0.45 <= elapsed < 2.0


# -------------------------------------------------------------- list + auth


def test_list_filters_by_overlap_and_hides_lock_ids(client):
    assert _acquire(client, "src", "parent-agent", mode="shared").status_code == 200
    assert _acquire(client, "src/auth", "A", mode="shared").status_code == 200
    assert _acquire(client, "docs/a.md", "B").status_code == 200
    data = client.get("/bus/locks?resource_prefix=src/auth/login.py").get_json()
    assert {lock["agent"] for lock in data["locks"]} == {"parent-agent", "A"}
    assert all("lock_id" not in lock for lock in data["locks"])
    assert client.get("/bus/locks").get_json()["count"] == 3
    assert client.get("/bus/locks?agent=B").get_json()["count"] == 1


@pytest.mark.parametrize(
    "body",
    [
        {"resource": "/abs/path.py", "agent": "A"},
        {"resource": "../x.py", "agent": "A"},
        {"resource": "src/app.py"},
        {"resource": "src/app.py", "agent": "A", "mode": "weird"},
        {"resource": "src/app.py", "agent": "A", "ttl_seconds": 0},
        {"resource": "src/app.py", "agent": "A", "ttl_seconds": "10"},
        {"resource": "src/app.py", "agent": "A", "wait_seconds": -1},
        {"resource": "src/app.py", "agent": "A", "reason": "x" * 501},
    ],
)
def test_acquire_validation_errors(client, body):
    assert client.post("/bus/locks/acquire", json=body).status_code == 400


def test_lock_endpoints_require_auth(monkeypatch):
    srv = _load_server(monkeypatch, token="secret")
    client = srv.app.test_client()
    body = {"resource": "src/app.py", "agent": "A"}
    assert client.post("/bus/locks/acquire", json=body).status_code == 401
    assert client.get("/bus/locks").status_code == 401
    assert client.post("/bus/locks/release", json={"lock_id": "x", "agent": "A"}).status_code == 401
    assert client.post("/bus/locks/renew", json={"lock_id": "x", "agent": "A"}).status_code == 401
    assert client.post("/bus/locks/validate", json={}).status_code == 401
    auth = {"Authorization": "Bearer secret"}
    assert client.post("/bus/locks/acquire", json=body, headers=auth).status_code == 200


def test_health_and_metrics_report_locks(client):
    _acquire(client, "src/app.py", "A")
    _acquire(client, "src/app.py", "B")
    health = client.get("/health").get_json()
    assert health["locks_held"] == 1
    assert health["locks_durable"] is False
    counters = client.get("/metrics").get_json()["counters"]
    assert counters["locks_acquired"] == 1
    assert counters["lock_conflicts"] == 1


# ------------------------------------------------------- JSON schema contract


def _schema_validator(def_name):
    import json

    from jsonschema import Draft202012Validator

    doc = json.loads((ROOT / "schemas" / "iac-bus.schema.json").read_text(encoding="utf-8"))
    schema = {"$schema": doc["$schema"], "$ref": f"#/$defs/{def_name}", "$defs": doc["$defs"]}
    return Draft202012Validator(schema)


def test_api_responses_match_schema(server, client):
    request_body = {"resource": "src/app.py", "agent": "A", "mode": "exclusive",
                    "ttl_seconds": 30, "reason": "edit", "wait_seconds": 0, "repo": "iac-bus"}
    _schema_validator("LockAcquireRequest").validate(request_body)

    lock = client.post("/bus/locks/acquire", json=request_body).get_json()
    _schema_validator("LockAcquireResponse").validate(lock)

    conflict = _acquire(client, "src/app.py", "B", repo="iac-bus").get_json()
    _schema_validator("LockConflict").validate(conflict)

    listing = client.get("/bus/locks").get_json()
    _schema_validator("LockList").validate(listing)

    validate_body = {"resource": "src/app.py", "fencing_token": lock["fencing_token"], "repo": "iac-bus"}
    _schema_validator("LockValidateRequest").validate(validate_body)
    checked = client.post("/bus/locks/validate", json=validate_body).get_json()
    _schema_validator("LockValidateResponse").validate(checked)

    renew_body = {"lock_id": lock["lock_id"], "agent": "A", "ttl_seconds": 30}
    _schema_validator("LockRenewRequest").validate(renew_body)
    release_body = {"lock_id": lock["lock_id"], "agent": "A"}
    _schema_validator("LockReleaseRequest").validate(release_body)
    client.post("/bus/locks/release", json=release_body)

    for event in _lock_events(client):
        _schema_validator("LockEvent").validate(event)


def test_schema_rejects_lock_id_in_public_views():
    from jsonschema import ValidationError

    view = {"repo": "r", "resource": "a", "mode": "shared", "agent": "A", "reason": "",
            "fencing_token": 1, "acquired_at": 1.0, "expires_at": 2.0, "ttl_seconds": 1.0}
    _schema_validator("Lock").validate(view)
    with pytest.raises(ValidationError):
        _schema_validator("Lock").validate(dict(view, lock_id="secret"))


def test_locks_work_with_sqlite_store(monkeypatch, tmp_path):
    monkeypatch.setenv("BUS_API_TOKEN", "")
    monkeypatch.setenv("IAC_BUS_STORE", "sqlite")
    monkeypatch.setenv("IAC_BUS_DB", str(tmp_path / "bus.db"))
    if "server" in sys.modules:
        del sys.modules["server"]
    import server  # noqa: F401
    srv = importlib.reload(sys.modules["server"])
    client = srv.app.test_client()
    lock = _acquire(client, "src/app.py", "A").get_json()
    assert _acquire(client, "src/app.py", "B").status_code == 409
    assert client.post("/bus/locks/release", json={"lock_id": lock["lock_id"], "agent": "A"}).status_code == 200
    assert [m["type"] for m in _lock_events(client)] == ["lock.acquired", "lock.released"]
    health = client.get("/health").get_json()
    assert health["locks_durable"] is False
    srv._store.close()
