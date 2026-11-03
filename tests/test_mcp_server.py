"""MCP adapter: tool behaviour with HTTP faked, plus the tools against the real bus app in-process."""

import importlib
import json
import sys
import threading
import time
from pathlib import Path
from urllib.parse import urlencode, urlparse

import pytest
import requests

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import mcp_server as m  # noqa: E402


# --------------------------------------------------------------- HTTP fakes


class FakeResponse:
    def __init__(self, status_code, body=None):
        self.status_code = status_code
        self._body = body
        self.content = b"" if body is None else json.dumps(body).encode()
        self.text = self.content.decode()

    def json(self):
        if self._body is None:
            raise ValueError("no body")
        return self._body


class FakeSession:
    """Records calls; replies from a queue per (method, path)."""

    def __init__(self):
        self.calls = []
        self.replies = {}

    def reply(self, method, path, status, body=None):
        self.replies.setdefault((method, path), []).append(FakeResponse(status, body))

    def request(self, method, url, json=None, params=None, headers=None, timeout=None):
        path = urlparse(url).path
        self.calls.append({"method": method, "path": path, "json": json, "params": params,
                           "headers": headers, "timeout": timeout})
        queue = self.replies.get((method, path))
        if not queue:
            raise AssertionError(f"unexpected call {method} {path}")
        return queue.pop(0)


class FlaskSession:
    """Routes BusClient calls into the Flask app in-process (no sockets)."""

    def __init__(self, app):
        self.app = app

    def request(self, method, url, json=None, params=None, headers=None, timeout=None):
        parsed = urlparse(url)
        path = parsed.path + (("?" + urlencode(params)) if params else "")
        client = self.app.test_client()
        resp = client.open(path, method=method, json=json, headers=headers)
        return FakeResponse(resp.status_code, resp.get_json(silent=True))


def _config(tmp_path=None, **over):
    base = dict(bus_url="http://bus.test", api_token="tok", agent_id="agent-a#abc123",
                repo="demo", repo_root=(tmp_path or Path("/repo")).resolve())
    base.update(over)
    return m.Config(**base)


def _tools(session, **over):
    cfg = _config(**over)
    return m.BusTools(m.BusClient(cfg, session=session), cfg)


# ------------------------------------------------------------- configuration


def test_agent_id_gets_unique_suffix_by_default():
    a = m.session_agent_id("claude.iac-bus.a")
    b = m.session_agent_id("claude.iac-bus.a")
    assert a.startswith("claude.iac-bus.a#") and b.startswith("claude.iac-bus.a#")
    assert a != b
    assert m.session_agent_id("claude.iac-bus.a", "0") == "claude.iac-bus.a"
    assert m.session_agent_id("", "0")  # falls back to user@host


def test_config_from_env(tmp_path):
    env = {"BUS_URL": "http://bus:8101/", "BUS_API_TOKEN": "t", "BUS_AGENT_ID": "x",
           "BUS_AGENT_SUFFIX": "off", "BUS_REPO": "iac-bus", "BUS_REPO_ROOT": str(tmp_path)}
    cfg = m.Config.from_env(env, cwd=tmp_path)
    assert cfg.bus_url == "http://bus:8101"
    assert cfg.agent_id == "x"
    assert cfg.repo == "iac-bus"
    assert cfg.repo_root == tmp_path.resolve()


def test_unexpanded_placeholders_count_as_unset(tmp_path):
    env = {"BUS_URL": "${BUS_URL}", "BUS_API_TOKEN": "${BUS_API_TOKEN}", "BUS_AGENT_ID": "${BUS_AGENT_ID}",
           "BUS_AGENT_SUFFIX": "${BUS_AGENT_SUFFIX}", "BUS_REPO": "x", "BUS_REPO_ROOT": str(tmp_path)}
    cfg = m.Config.from_env(env, cwd=tmp_path)
    assert cfg.bus_url == m.DEFAULT_BUS_URL
    assert cfg.api_token == ""
    assert "${" not in cfg.agent_id and "#" in cfg.agent_id


def test_claude_project_dir_sets_repo_root(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    cfg = m.Config.from_env({"CLAUDE_PROJECT_DIR": str(project), "BUS_REPO": "x"}, cwd=elsewhere)
    assert cfg.repo_root == project.resolve()


def test_repo_defaults_to_repo_root_name(tmp_path):
    root = tmp_path / "my-repo"
    root.mkdir()
    cfg = m.Config.from_env({"BUS_REPO_ROOT": str(root)}, cwd=root)
    assert cfg.repo == "my-repo"


@pytest.mark.parametrize("url,name", [
    ("git@github.com:lehelkovach/iac-bus.git", "iac-bus"),
    ("https://github.com/lehelkovach/iac-bus", "iac-bus"),
    ("http://proxy@127.0.0.1:1234/git/lehelkovach/iac-bus/", "iac-bus"),
    ("/srv/git/knowshowgo.git", "knowshowgo"),
])
def test_repo_name_from_remote(url, name):
    assert m.repo_name_from_remote(url) == name


def test_worktrees_share_the_repo_scope(tmp_path):
    import subprocess

    main = tmp_path / "checkout"
    main.mkdir()
    run = lambda *a, cwd=main: subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True)  # noqa: E731
    run("init", "-q")
    run("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "init")
    run("worktree", "add", "-q", str(tmp_path / "agent-b-tree"))
    assert m.detect_repo_name(main) == "checkout"
    assert m.detect_repo_name(tmp_path / "agent-b-tree") == "checkout"
    run("remote", "add", "origin", "git@github.com:me/real-name.git")
    assert m.detect_repo_name(tmp_path / "agent-b-tree") == "real-name"


def test_absolute_paths_become_repo_relative(tmp_path):
    tools = _tools(FakeSession(), repo_root=tmp_path)
    assert tools.to_resource(str(tmp_path / "src" / "app.py")) == "src/app.py"
    assert tools.to_resource("./src/auth/") == "src/auth"
    with pytest.raises(ValueError):
        tools.to_resource("/etc/passwd")


# ---------------------------------------------------------------- lock tools


def _lock_body(**over):
    body = {"lock_id": "L1", "repo": "demo", "resource": "src/app.py", "mode": "exclusive",
            "agent": "agent-a#abc123", "reason": "", "fencing_token": 41, "acquired_at": time.time(),
            "expires_at": time.time() + 600, "ttl_seconds": 600, "waited_seconds": 0}
    body.update(over)
    return body


def test_acquire_sends_identity_repo_and_auth():
    s = FakeSession()
    s.reply("POST", "/bus/locks/acquire", 200, _lock_body())
    tools = _tools(s)
    out = tools.acquire_lock("src/app.py", reason="refactor", wait_seconds=99)
    call = s.calls[0]
    assert call["json"]["agent"] == "agent-a#abc123"
    assert call["json"]["repo"] == "demo"
    assert call["json"]["reason"] == "refactor"
    assert call["json"]["wait_seconds"] == 30  # clamped
    assert call["headers"]["Authorization"] == "Bearer tok"
    assert call["timeout"] >= 30
    assert out["acquired"] is True and out["fencing_token"] == 41
    assert tools.held_locks()[0]["lock_id"] == "L1"


def test_acquire_conflict_tells_agent_not_to_edit():
    s = FakeSession()
    s.reply("POST", "/bus/locks/acquire", 409, {
        "error": "lock conflict", "held_by": "agent-b#999", "mode": "exclusive", "reason": "auth fix",
        "expires_at": time.time() + 60, "conflicting_resource": "src", "conflicts": [{}], "waited_seconds": 0})
    tools = _tools(s)
    out = tools.acquire_lock("src/app.py")
    assert out["acquired"] is False
    assert out["held_by"] == "agent-b#999"
    assert out["conflicting_path"] == "src"
    assert "Do NOT edit" in out["next"]
    assert tools.held_locks() == []


def test_release_by_path_and_all():
    s = FakeSession()
    s.reply("POST", "/bus/locks/acquire", 200, _lock_body(lock_id="L1", resource="src/app.py"))
    s.reply("POST", "/bus/locks/acquire", 200, _lock_body(lock_id="L2", resource="docs"))
    s.reply("POST", "/bus/locks/release", 200, {"success": True, "lock": {"resource": "src/app.py"}})
    s.reply("POST", "/bus/locks/release", 404, {"error": "lock not found or expired"})
    tools = _tools(s)
    tools.acquire_lock("src/app.py")
    tools.acquire_lock("docs/")
    assert tools.release_lock(path="./src/app.py") == {"released": ["src/app.py"]}
    assert s.calls[2]["json"] == {"lock_id": "L1", "agent": "agent-a#abc123"}
    out = tools.release_lock(all=True)
    assert out == {"released": [], "already_expired": ["docs"]}
    assert tools.held_locks() == []
    with pytest.raises(ValueError):
        tools.release_lock(path="nothing/here.py")


def test_renew_uses_tracked_lock_id():
    s = FakeSession()
    s.reply("POST", "/bus/locks/acquire", 200, _lock_body())
    s.reply("POST", "/bus/locks/renew", 200, _lock_body(expires_at=time.time() + 900))
    tools = _tools(s)
    tools.acquire_lock("src/app.py")
    out = tools.renew_lock(path="src/app.py", ttl_seconds=900)
    assert s.calls[1]["json"]["lock_id"] == "L1"
    assert out["renewed"][0]["path"] == "src/app.py"


def test_check_lock_uses_held_token_and_reports_stale():
    s = FakeSession()
    s.reply("POST", "/bus/locks/acquire", 200, _lock_body(resource="src", fencing_token=7))
    s.reply("POST", "/bus/locks/validate", 200, {"valid": True, "reason": "ok", "lock": {"mode": "exclusive"}})
    s.reply("POST", "/bus/locks/validate", 409, {"valid": False, "reason": "no live lock"})
    tools = _tools(s)
    tools.acquire_lock("src/")
    assert tools.check_lock("src/app.py")["valid"] is True
    assert s.calls[1]["json"]["fencing_token"] == 7
    stale = tools.check_lock("src/app.py")
    assert stale["valid"] is False and "Stop" in stale["next"]
    assert tools.check_lock("docs/x.md")["valid"] is False  # no covering lock, no HTTP call
    assert len(s.calls) == 3


def test_list_locks_scopes_to_repo():
    s = FakeSession()
    s.reply("GET", "/bus/locks", 200, {"locks": [
        {"repo": "demo", "resource": "src", "mode": "exclusive", "agent": "agent-a#abc123",
         "reason": "", "expires_at": time.time() + 10}], "count": 1})
    out = _tools(s).list_locks(path_prefix="src/app.py", mine_only=True)
    assert s.calls[0]["params"] == {"repo": "demo", "resource_prefix": "src/app.py", "agent": "agent-a#abc123"}
    assert out["locks"][0]["mine"] is True


# ----------------------------------------------------------------- work tools


def test_claim_then_ack_without_repeating_lease():
    s = FakeSession()
    s.reply("POST", "/bus/queues/claim", 204)
    s.reply("POST", "/bus/queues/claim", 200, {"message": {
        "id": "M1", "lease_id": "LEASE", "lease_until": time.time() + 300, "type": "task",
        "sender": "lead", "message": {"do": "x"}}})
    s.reply("POST", "/bus/queues/ack", 200, {"success": True})
    tools = _tools(s)
    assert tools.claim_work("work")["claimed"] is False
    claimed = tools.claim_work("work")
    assert claimed["work"] == {"do": "x"}
    assert s.calls[1]["json"]["worker"] == "agent-a#abc123"
    assert tools.ack_work("M1") == {"ok": True, "message_id": "M1", "queue": "work"}
    assert s.calls[2]["json"]["lease_id"] == "LEASE"


def test_nack_reports_lease_mismatch():
    s = FakeSession()
    s.reply("POST", "/bus/queues/nack", 409, {"error": "lease id mismatch"})
    out = _tools(s).nack_work("M1", lease_id="bad", queue="work")
    assert out == {"ok": False, "message_id": "M1", "error": "lease id mismatch"}
    assert s.calls[0]["json"]["requeue"] is True


# ----------------------------------------------------------- messages, status


def test_post_and_read_continue_from_cursor():
    s = FakeSession()
    s.reply("POST", "/bus/messages", 201, {"success": True, "message": {"id": "m1", "sender": "agent-a#abc123"}})
    s.reply("GET", "/bus/messages", 200, {"messages": [{"id": "m1", "ts": 1, "sender": "x", "type": "info", "message": "hi"}]})
    s.reply("GET", "/bus/messages", 200, {"messages": []})
    tools = _tools(s)
    tools.post_message("ops", "hello", recipient="agent-b")
    assert s.calls[0]["json"]["sender"] == "agent-a#abc123"
    assert s.calls[0]["json"]["recipient"] == "agent-b"
    assert "conversation_id" not in s.calls[0]["json"]
    first = tools.read_messages("ops")
    assert first["next_since_id"] == "m1"
    tools.read_messages("ops", wait_seconds=5)
    assert s.calls[2]["params"] == {"channel": "ops", "limit": 50, "since_id": "m1", "wait_seconds": 5.0}


def test_request_decision_posts_agent_contract_and_finds_answer():
    s = FakeSession()
    s.reply("POST", "/bus/messages", 201, {"success": True, "message": {"id": "req1", "sender": "agent-a#abc123"}})
    tools = _tools(s)

    def answer_for(conv):
        return {"messages": [
            {"id": "req1", "type": "request_decision", "conversation_id": conv, "message": {}},
            {"id": "ans1", "type": "provide_decision", "sender": "human", "conversation_id": conv,
             "message": {"kind": "response", "action": "provide_decision", "conversation_id": conv,
                         "payload": {"choice_id": "continue", "approved": True, "notes": "go"}}},
        ]}

    real_request = s.request

    def request(method, url, **kw):
        if method == "GET":
            conv = s.calls[0]["json"]["conversation_id"]
            s.calls.append({"method": method, "path": urlparse(url).path, **kw})
            return FakeResponse(200, answer_for(conv))
        return real_request(method, url, **kw)

    s.request = request
    out = tools.request_decision("Delete the legacy module?", options=["Continue", "Abort"], wait_seconds=5)
    posted = s.calls[0]["json"]
    assert posted["type"] == "request_decision"
    assert posted["message"]["action"] == "request_decision"
    assert posted["message"]["from"] == "agent-a#abc123"
    assert posted["message"]["state"] == "waiting_input"
    assert [c["id"] for c in posted["message"]["payload"]["choices"]] == ["continue", "abort"]
    assert out["decided"] is True
    assert out["choice_id"] == "continue" and out["approved"] is True
    assert out["decided_by"] == "human"


def test_check_decision_times_out_undecided():
    s = FakeSession()
    s.reply("GET", "/bus/messages", 200, {"messages": []})
    out = _tools(s).check_decision("nope", wait_seconds=0)
    assert out["decided"] is False


def test_report_status_validates_state_and_posts_envelope():
    s = FakeSession()
    s.reply("POST", "/bus/messages", 201, {"success": True, "message": {"id": "s1", "sender": "agent-a#abc123"}})
    tools = _tools(s)
    with pytest.raises(ValueError):
        tools.report_status("vibing", "x")
    tools.report_status("running", "refactoring auth", progress_pct=10, task="T-1")
    body = s.calls[0]["json"]
    assert body["channel"] == "status" and body["type"] == "report_status"
    assert body["message"]["action"] == "report_status"
    assert body["message"]["payload"]["state"] == "running"
    assert body["message"]["payload"]["details"] == {"task": "T-1"}


# --------------------------------------------------------------- HTTP errors


def test_unauthorized_and_unreachable_raise_bus_error():
    s = FakeSession()
    s.reply("GET", "/bus/locks", 401, {"error": "Unauthorized"})
    with pytest.raises(m.BusError, match="BUS_API_TOKEN"):
        _tools(s).list_locks()

    class Down:
        def request(self, *a, **kw):
            raise requests.ConnectionError("refused")

    with pytest.raises(m.BusError, match="cannot reach the bus"):
        _tools(Down()).list_locks()


def test_bad_request_details_surface():
    s = FakeSession()
    s.reply("POST", "/bus/locks/acquire", 400, {"error": "invalid lock request", "details": ["mode must be ..."]})
    with pytest.raises(m.BusError, match="mode must be"):
        _tools(s).acquire_lock("src/app.py", mode="weird")


# ------------------------------------------------------------ MCP surface


def test_mcp_server_lists_tools_with_lock_guidance():
    pytest.importorskip("mcp")
    import anyio

    tools = _tools(FakeSession())
    server = m.build_server(tools)

    async def run():
        return await server.list_tools()

    listed = {t.name: t for t in anyio.run(run)}
    assert set(listed) == set(m.TOOL_DESCRIPTIONS)
    assert {"acquire_lock", "release_lock", "renew_lock", "list_locks", "claim_work", "ack_work",
            "nack_work", "post_message", "read_messages", "request_decision", "report_status"} <= set(listed)
    acquire = listed["acquire_lock"].description
    assert "BEFORE EDITING" in acquire and "release_lock" in acquire
    assert "finished editing" in listed["release_lock"].description
    assert "acquire_lock before editing" in listed["list_locks"].description
    assert "Acquire locks" in listed["claim_work"].description
    assert "BEFORE you edit" in server.instructions
    assert tools.agent_id in server.instructions


def test_mcp_tool_errors_are_reported_not_raised():
    pytest.importorskip("mcp")
    import anyio
    from mcp import Client

    tools = _tools(FakeSession())
    server = m.build_server(tools)

    async def run():
        async with Client(server) as client:
            return await client.call_tool("release_lock", {})

    result = anyio.run(run)
    assert result.is_error
    assert "pass path, lock_id, or all=true" in result.content[0].text


# ------------------------------------------- end to end against the real bus


@pytest.fixture
def bus_app(monkeypatch):
    monkeypatch.setenv("BUS_API_TOKEN", "")
    monkeypatch.delenv("IAC_BUS_STORE", raising=False)
    if "server" in sys.modules:
        del sys.modules["server"]
    import server  # noqa: F401
    srv = importlib.reload(sys.modules["server"])
    srv._bus_messages.clear()
    return srv.app


def _bus_tools(app, agent):
    cfg = _config(agent_id=agent, api_token="")
    return m.BusTools(m.BusClient(cfg, session=FlaskSession(app)), cfg)


def test_two_agents_through_the_tools(bus_app):
    a = _bus_tools(bus_app, "agent-a#1")
    b = _bus_tools(bus_app, "agent-b#2")

    got = a.acquire_lock("src/app.py", reason="refactor")
    assert got["acquired"] is True

    blocked = b.acquire_lock("src/app.py")
    assert blocked["acquired"] is False and blocked["held_by"] == "agent-a#1"

    result = {}
    waiter = threading.Thread(target=lambda: result.update(b.acquire_lock("src/app.py", wait_seconds=5)))
    waiter.start()
    time.sleep(0.3)
    assert a.check_lock("src/app.py")["valid"] is True
    assert a.release_lock(path="src/app.py") == {"released": ["src/app.py"]}
    waiter.join(timeout=5)
    assert result["acquired"] is True
    assert result["fencing_token"] > got["fencing_token"]

    assert a.check_lock("src/app.py", fencing_token=got["fencing_token"])["valid"] is False
    assert b.check_lock("src/app.py")["valid"] is True
    assert b.list_locks()["locks"][0]["held_by"] == "agent-b#2"
    assert b.release_lock(all=True)["released"] == ["src/app.py"]

    events = [msg["type"] for msg in a.read_messages("locks")["messages"]]
    assert events == ["lock.acquired", "lock.released", "lock.acquired", "lock.released"]


def test_decision_round_trip_through_the_bus(bus_app):
    worker = _bus_tools(bus_app, "worker#1")
    asked = worker.request_decision("Drop table users?", options=["Yes", "No"])
    assert asked["decided"] is False

    human = bus_app.test_client()
    human.post("/bus/messages", json={
        "channel": "decisions", "sender": "lehel", "type": "provide_decision",
        "conversation_id": asked["conversation_id"],
        "message": {"kind": "response", "action": "provide_decision",
                    "conversation_id": asked["conversation_id"],
                    "payload": {"choice_id": "no", "approved": False, "notes": "never"}},
    })
    answer = worker.check_decision(asked["conversation_id"])
    assert answer == {"choice_id": "no", "approved": False, "notes": "never", "decided": True, "decided_by": "lehel"}


def test_work_queue_through_the_tools(bus_app):
    lead = bus_app.test_client()
    lead.post("/bus/messages", json={"channel": "work", "queue": "work", "sender": "lead", "message": {"task": "fix"}})
    worker = _bus_tools(bus_app, "worker#1")
    claimed = worker.claim_work("work")
    assert claimed["claimed"] is True and claimed["work"] == {"task": "fix"}
    assert worker.ack_work(claimed["message_id"])["ok"] is True
    assert worker.claim_work("work")["claimed"] is False


# ------------------------------------------------- run tokens (Phase 2b)


def _register_reply(handle="agent-a", run_id="abcdef0123456789", token="iacrun_T1"):
    return {"success": True, "agent": {"agent_uuid": "u"}, "run": {"run_id": run_id},
            "actor": f"{handle}#{run_id[:6]}", "run_token": token}


def test_start_run_registers_and_switches_to_run_token():
    s = FakeSession()
    s.reply("POST", "/agents/register", 201, _register_reply())
    s.reply("GET", "/bus/locks", 200, {"locks": [], "count": 0})
    tools = _tools(s, agent_id="agent-a#zzzzzz")
    assert tools.start_run() == "run token (registered at startup)"
    reg = s.calls[0]
    assert reg["json"]["handle"] == "agent-a"  # the handle, without the local suffix
    assert reg["headers"]["Authorization"] == "Bearer tok"  # shared token registers
    assert tools.agent_id == "agent-a#abcdef"
    tools.list_locks()
    assert s.calls[1]["headers"]["Authorization"] == "Bearer iacrun_T1"


def test_start_run_falls_back_on_old_or_down_bus():
    s = FakeSession()
    s.reply("POST", "/agents/register", 201, {"success": True, "agent": {"agent_uuid": "u"}})
    tools = _tools(s, agent_id="agent-a#zzzzzz")
    assert tools.start_run() == "name (shared token)"
    assert tools.agent_id == "agent-a#zzzzzz"
    assert tools.client.run_token == ""

    class Down:
        def request(self, *a, **kw):
            raise requests.ConnectionError("refused")

    down = _tools(Down(), agent_id="agent-a#zzzzzz")
    assert "registration failed" in down.start_run()
    assert down.agent_id == "agent-a#zzzzzz"


def test_start_run_with_launcher_token_uses_whoami():
    s = FakeSession()
    s.reply("GET", "/agents/whoami", 200, {"auth": "run", "actor": "worker#123456", "run": {"run_id": "123456ff"}})
    tools = _tools(s, run_token="iacrun_FROM_LAUNCHER")
    assert tools.start_run() == "run token (BUS_RUN_TOKEN)"
    assert s.calls[0]["headers"]["Authorization"] == "Bearer iacrun_FROM_LAUNCHER"
    assert tools.agent_id == "worker#123456"


def test_rejected_run_token_reregisters_and_retries():
    s = FakeSession()
    s.reply("POST", "/agents/register", 201, _register_reply(token="iacrun_OLD"))
    s.reply("GET", "/bus/locks", 401, {"error": "Unauthorized", "reason": "unknown run token"})
    s.reply("POST", "/agents/register", 201, _register_reply(run_id="999999aaaa", token="iacrun_NEW"))
    s.reply("GET", "/bus/locks", 200, {"locks": [], "count": 0})
    tools = _tools(s)
    tools.start_run()
    out = tools.list_locks()
    assert out["you"] == "agent-a#999999"
    assert s.calls[3]["headers"]["Authorization"] == "Bearer iacrun_NEW"


def test_exit_ends_the_run():
    s = FakeSession()
    s.reply("POST", "/agents/register", 201, _register_reply())
    s.reply("POST", "/agents/runs/end", 200, {"success": True})
    tools = _tools(s)
    tools.start_run()
    tools.release_all_quietly()
    assert s.calls[-1]["path"] == "/agents/runs/end"
    assert s.calls[-1]["headers"]["Authorization"] == "Bearer iacrun_T1"


def test_config_reads_run_settings(tmp_path):
    cfg = m.Config.from_env({"BUS_REPO": "x", "BUS_REPO_ROOT": str(tmp_path), "BUS_AGENT_ID": "h",
                             "BUS_RUN_REGISTER": "0", "BUS_RUN_TOKEN": "iacrun_x"}, cwd=tmp_path)
    assert cfg.agent_handle == "h" and cfg.agent_id.startswith("h#")
    assert cfg.register_run is False and cfg.run_token == "iacrun_x"


@pytest.fixture
def secured_bus(monkeypatch):
    monkeypatch.setenv("BUS_API_TOKEN", "s3cret")
    monkeypatch.delenv("IAC_BUS_STORE", raising=False)
    if "server" in sys.modules:
        del sys.modules["server"]
    import server  # noqa: F401
    srv = importlib.reload(sys.modules["server"])
    srv._bus_messages.clear()
    return srv.app


def test_run_identity_end_to_end(secured_bus):
    def adapter(handle):
        cfg = _config(agent_id=f"{handle}#local1", api_token="s3cret")
        tools = m.BusTools(m.BusClient(cfg, session=FlaskSession(secured_bus)), cfg)
        assert tools.start_run() == "run token (registered at startup)"
        return tools

    a, b = adapter("claude.a"), adapter("claude.b")
    assert a.agent_id.startswith("claude.a#") and a.agent_id != "claude.a#local1"

    got = a.acquire_lock("src/app.py")
    assert got["acquired"]
    # B learns A's lock id and pretends to be A: the bus derives B from its run token.
    b.agent_id = a.agent_id
    with pytest.raises(m.BusError):
        b.release_lock(lock_id=got["lock_id"], path="src/app.py")
    assert b.acquire_lock("src/app.py")["held_by"] == a.agent_id

    a.release_all_quietly()  # releases locks and ends A's run
    assert b.acquire_lock("src/app.py")["acquired"] is True
    whoami = secured_bus.test_client().get("/agents/whoami", headers={"Authorization": "Bearer " + b.client.run_token})
    assert whoami.get_json()["actor"].startswith("claude.b#")
