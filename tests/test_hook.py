"""The pre-commit hook refuses commits whose staged files lack a live exclusive lock."""

import importlib
import json
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path

import pytest
import requests
from werkzeug.serving import make_server

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import mcp_server as m  # noqa: E402

HOOK = ROOT / "scripts" / "hooks" / "pre-commit"


def git(repo, *args, env=None):
    base = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
            "PATH": os.environ["PATH"], "HOME": os.environ.get("HOME", "/tmp")}
    base.update(env or {})
    return subprocess.run(["git", *args], cwd=repo, env=base, capture_output=True, text=True)


@pytest.fixture
def bus(monkeypatch):
    monkeypatch.setenv("BUS_API_TOKEN", "shared-secret")
    monkeypatch.delenv("IAC_BUS_STORE", raising=False)
    if "server" in sys.modules:
        del sys.modules["server"]
    import server  # noqa: F401
    srv = importlib.reload(sys.modules["server"])
    srv._bus_messages.clear()
    httpd = make_server("127.0.0.1", 0, srv.app, threaded=True)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_port}"
    httpd.shutdown()


@pytest.fixture
def repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    assert git(repo, "init", "-q").returncode == 0
    (repo / "src").mkdir()
    (repo / "src" / "app.py").write_text("x = 1\n")
    (repo / "README.md").write_text("hi\n")
    git(repo, "add", "-A")
    assert git(repo, "commit", "-qm", "init").returncode == 0
    assert subprocess.run(["bash", str(ROOT / "scripts" / "install_hooks.sh"), str(repo)],
                          capture_output=True, text=True).returncode == 0
    return repo


def _tools(bus_url, repo):
    cfg = m.Config(bus_url=bus_url, api_token="shared-secret", agent_id="hooked#1", repo="demo", repo_root=repo,
                   auto_status=False)
    session = requests.Session()
    session.trust_env = False
    tools = m.BusTools(m.BusClient(cfg, session=session), cfg)
    tools.start_run()
    return tools


def _stage_and_commit(repo, path, text, env=None):
    (repo / path).write_text(text)
    git(repo, "add", path)
    return git(repo, "commit", "-qm", f"edit {path}", env=env)


def test_lock_file_follows_held_locks(bus, repo):
    tools = _tools(bus, repo)
    lock_file = repo / ".git" / "iac-bus-locks.json"
    tools.acquire_lock("src/app.py")
    state = json.loads(lock_file.read_text())
    assert oct(lock_file.stat().st_mode & 0o777) == "0o600"
    assert state["agent"] == tools.agent_id and state["repo"] == "demo"
    assert [lock["resource"] for lock in state["locks"]] == ["src/app.py"]
    assert state["token"].startswith("iacrun_") and "lock_id" not in json.dumps(state["locks"])
    tools.release_lock(path="src/app.py")
    assert json.loads(lock_file.read_text())["locks"] == []


def test_commit_without_lock_file_is_allowed_unless_strict(repo):
    assert _stage_and_commit(repo, "README.md", "human edit\n").returncode == 0
    result = _stage_and_commit(repo, "README.md", "strict\n", env={"IAC_BUS_HOOK_STRICT": "1"})
    assert result.returncode != 0 and "no lock file" in result.stderr


def test_commit_needs_a_covering_live_exclusive_lock(bus, repo):
    tools = _tools(bus, repo)
    tools.acquire_lock("src")
    refused = _stage_and_commit(repo, "README.md", "unlocked\n")
    assert refused.returncode != 0 and "no lock held" in refused.stderr
    git(repo, "reset", "-q", "README.md")
    (repo / "README.md").write_text("hi\n")

    ok = _stage_and_commit(repo, "src/app.py", "x = 2\n")
    assert ok.returncode == 0, ok.stderr
    assert "covered by live locks" in ok.stdout + ok.stderr

    tools.release_lock(all=True)
    tools.acquire_lock("src", mode="shared")
    shared = _stage_and_commit(repo, "src/app.py", "x = 3\n")
    assert shared.returncode != 0 and "shared lock" in shared.stderr
    git(repo, "reset", "-q", "src/app.py")

    skipped = _stage_and_commit(repo, "src/app.py", "x = 3\n", env={"IAC_BUS_HOOK_SKIP": "1"})
    assert skipped.returncode == 0


def test_commit_is_refused_after_the_lock_was_lost(bus, repo):
    tools = _tools(bus, repo)
    tools.acquire_lock("src/app.py", ttl_seconds=1)
    import time
    time.sleep(1.2)
    other = _tools(bus, repo.parent)  # another session takes the path over
    other.agent_id = "taker#2"
    assert other.acquire_lock("src/app.py")["acquired"] is True
    # the first session still believes it holds the lock; the hook asks the bus
    result = _stage_and_commit(repo, "src/app.py", "stale\n")
    assert result.returncode != 0 and "not live" in result.stderr


def test_installer_refuses_to_clobber_a_foreign_hook(tmp_path):
    repo = tmp_path / "r"
    repo.mkdir()
    git(repo, "init", "-q")
    hooks = repo / ".git" / "hooks"
    hooks.mkdir(exist_ok=True)
    (hooks / "pre-commit").write_text("#!/bin/sh\necho mine\n")
    result = subprocess.run(["bash", str(ROOT / "scripts" / "install_hooks.sh"), str(repo)], capture_output=True, text=True)
    assert result.returncode == 1 and "different pre-commit hook" in result.stderr
    forced = subprocess.run(["bash", str(ROOT / "scripts" / "install_hooks.sh"), str(repo), "--force"],
                            capture_output=True, text=True)
    assert forced.returncode == 0 and "iac-bus pre-commit hook" in (hooks / "pre-commit").read_text()
    assert shutil.which("git")
