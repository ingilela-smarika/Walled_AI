import json
import os
import asyncio

import pytest
from aiohttp import ClientSession, web
from aiohttp.test_utils import TestServer

from mcpguard import install
from mcpguard.daemon import ApprovalDaemon, HeadlessUI
from mcpguard.http_proxy import HttpGuardProxy


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("MCPGUARD_HOME", str(tmp_path / "home"))


def rpc(i, method="tools/call"):
    return {"jsonrpc": "2.0", "id": i, "method": method, "params": {"name": "send", "arguments": {"a": 1}}}


async def _roundtrip(allow: bool):
    executed = []

    async def upstream(request):
        msg = await request.json()
        if msg["method"] == "tools/call":
            executed.append(msg["id"])
        return web.json_response({"jsonrpc": "2.0", "id": msg["id"], "result": {"ok": True}})

    up_app = web.Application()
    up_app.router.add_post("/mcp", upstream)
    d = ApprovalDaemon(HeadlessUI(allow), timeout=5, port=0)
    d.start()
    try:
        async with TestServer(up_app) as up:
            proxy = HttpGuardProxy(str(up.make_url("")), path_secret="s3cret")
            async with TestServer(proxy.make_app()) as px:
                async with ClientSession() as c:
                    ok = await asyncio.gather(*(c.post(px.make_url("/s3cret/mcp"), json=rpc(i))
                                                for i in range(1, 6)))
                    bodies = [await r.json() for r in ok]
                    wrong = await c.post(px.make_url("/mcp"), json=rpc(9))
                    listing = await c.post(px.make_url("/s3cret/mcp"), json=rpc(10, "tools/list"))
                    return bodies, executed, wrong.status, (await listing.json())
    finally:
        d.stop()


def test_http_allow():
    bodies, executed, wrong, listing = asyncio.run(_roundtrip(True))
    assert all(b["result"] == {"ok": True} for b in bodies)
    assert sorted(executed) == [1, 2, 3, 4, 5]
    assert wrong == 404                      # missing secret prefix
    assert listing["result"] == {"ok": True}  # tools/list is not gated


def test_http_block():
    bodies, executed, _, listing = asyncio.run(_roundtrip(False))
    assert all(b["result"]["isError"] for b in bodies)
    assert executed == []
    assert listing["result"] == {"ok": True}  # non-gated still flows


def test_install_roundtrip(tmp_path):
    cfg = tmp_path / "c.json"
    original = {"mcpServers": {"fs": {"command": "npx", "args": ["-y", "pkg", "/tmp"], "env": {"A": "1"}},
                               "bare": {"command": "uvx"}}}
    cfg.write_text(json.dumps(original))
    assert sorted(install.apply(cfg)) == ["bare", "fs"]
    assert install.apply(cfg) == []                       # idempotent
    wrapped = json.loads(cfg.read_text())["mcpServers"]["fs"]
    assert wrapped["args"][-4:] == ["npx", "-y", "pkg", "/tmp"] and wrapped["env"] == {"A": "1"}
    assert sorted(install.apply(cfg, undo=True)) == ["bare", "fs"]
    assert json.loads(cfg.read_text()) == original
    assert (tmp_path / "c.json.mcpguard.bak").exists()
