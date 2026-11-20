import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from mcpguard.daemon import ApprovalDaemon, HeadlessUI
from mcpguard.protocol import denial_for, is_gated

ROOT = Path(__file__).resolve().parent.parent
FAKE = Path(__file__).with_name("fake_server.py")


def call(i, name="read_file"):
    return {"jsonrpc": "2.0", "id": i, "method": "tools/call",
            "params": {"name": name, "arguments": {"path": "/tmp/x"}}}


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("MCPGUARD_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("FAKE_LOG", str(tmp_path / "executed.log"))
    return dict(os.environ, PYTHONPATH=str(ROOT))


def run_proxy(env, messages):
    """Talk to the proxy like Claude does: keep stdin open until all replies arrive."""
    import threading
    p = subprocess.Popen([sys.executable, "-m", "mcpguard", "stdio", "--name", "fake", "--",
                          sys.executable, str(FAKE)],
                         stdin=subprocess.PIPE, stdout=subprocess.PIPE, env=env, cwd=ROOT)
    watchdog = threading.Timer(20, p.kill)
    watchdog.start()
    try:
        for m in messages:
            p.stdin.write((json.dumps(m) + "\n").encode())
        p.stdin.flush()
        replies = {}
        for _ in messages:
            r = json.loads(p.stdout.readline())
            replies[r["id"]] = r
        return replies
    finally:
        watchdog.cancel()
        p.stdin.close()
        try:
            p.wait(10)
        except subprocess.TimeoutExpired:
            p.kill()


def executed(env):
    f = Path(env["FAKE_LOG"])
    return f.read_text().split() if f.exists() else []


def test_protocol_helpers():
    assert is_gated(call(1)) and not is_gated({"method": "tools/list", "id": 1})
    assert not is_gated({"method": "tools/call"})  # notification: no id
    r = denial_for({"method": "resources/read", "id": 3}, "nope")
    assert r["error"]["code"] == -32001


def test_allow_forwards(env):
    d = ApprovalDaemon(HeadlessUI(True), timeout=5, port=0); d.start()
    try:
        out = run_proxy(env, [call(1)])
    finally:
        d.stop()
    assert out[1]["result"]["content"][0]["text"] == "ran:read_file"
    assert executed(env) == ["read_file"]


def test_block_never_reaches_server(env):
    d = ApprovalDaemon(HeadlessUI(False), timeout=5, port=0); d.start()
    try:
        out = run_proxy(env, [call(1), {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}])
    finally:
        d.stop()
    assert out[1]["result"]["isError"] is True
    assert "blocked" in out[1]["result"]["content"][0]["text"]
    assert 2 in out                      # non-gated traffic still flows
    assert executed(env) == []


def test_concurrent_requests(env):
    d = ApprovalDaemon(HeadlessUI(True), timeout=5, port=0); d.start()
    try:
        out = run_proxy(env, [call(i, f"t{i}") for i in range(1, 21)])
    finally:
        d.stop()
    assert sorted(out) == list(range(1, 21))
    assert sorted(executed(env)) == sorted(f"t{i}" for i in range(1, 21))


def test_fails_closed_when_daemon_down(env):
    out = run_proxy(env, [call(1)])
    assert out[1]["result"]["isError"] is True
    assert executed(env) == []
