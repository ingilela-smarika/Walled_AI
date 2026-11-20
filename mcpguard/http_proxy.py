"""Streamable-HTTP reverse proxy for *remote* MCP servers (Claude Web connectors).

Claude Web calls remote MCP servers from Anthropic's cloud, so nothing on the user's
machine sees that traffic. The only interception point is the server endpoint
itself: this proxy sits in front of the real server, and the *proxy's* public URL
is what gets registered as the custom connector in Claude.

    Claude (cloud) -> https://<tunnel>/<secret>/mcp -> MCPGuard -> real server
                                                         |  popup on user's desktop

POST bodies are inspected; GET (SSE streams), DELETE and everything else is
streamed through unchanged. A gated JSON-RPC request is held until the user
decides, then forwarded or answered locally with a denial.
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse

import aiohttp
from aiohttp import web

from .client import request_approval
from .protocol import denial_for, describe, gated_in

log = logging.getLogger("mcpguard.http")

_HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te",
        "trailers", "transfer-encoding", "upgrade", "host", "content-length"}


class HttpGuardProxy:
    def __init__(self, upstream: str, server_name: str | None = None,
                 source: str = "claude-web", path_secret: str | None = None):
        self.upstream = upstream.rstrip("/")
        self.server_name = server_name or urlparse(upstream).netloc
        self.source = source
        self.path_secret = path_secret.strip("/") if path_secret else None
        self.pool = ThreadPoolExecutor(max_workers=64, thread_name_prefix="approval")
        self.session: aiohttp.ClientSession | None = None

    def make_app(self) -> web.Application:
        app = web.Application(client_max_size=64 * 1024 * 1024)
        app.cleanup_ctx.append(self._session_ctx)
        app.router.add_route("*", "/{tail:.*}", self.handle)
        return app

    async def _session_ctx(self, app):
        self.session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=None, sock_connect=15))
        yield
        await self.session.close()
        self.pool.shutdown(wait=False, cancel_futures=True)

    async def handle(self, request: web.Request) -> web.StreamResponse:
        path = request.rel_url.raw_path
        if self.path_secret:
            prefix = "/" + self.path_secret
            if not (path == prefix or path.startswith(prefix + "/")):
                return web.Response(status=404)
            path = path[len(prefix):] or "/"
        qs = request.rel_url.raw_query_string
        url = self.upstream + path + (("?" + qs) if qs else "")
        body = await request.read()

        if request.method == "POST" and body:
            denial = await self._gate(body)
            if denial is not None:
                return web.json_response(denial)

        headers = {k: v for k, v in request.headers.items()
                   if k.lower() not in _HOP and k.lower() != "accept-encoding"}
        prepared = False
        resp = web.StreamResponse()
        try:
            async with self.session.request(request.method, url, headers=headers,
                                            data=body or None, allow_redirects=False) as up:
                resp.set_status(up.status, up.reason)
                for k, v in up.headers.items():
                    if k.lower() not in _HOP and k.lower() != "content-encoding":
                        resp.headers.add(k, v)
                await resp.prepare(request)
                prepared = True
                async for chunk in up.content.iter_any():
                    await resp.write(chunk)
                await resp.write_eof()
                return resp
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            log.error("upstream error: %s", exc)
            if prepared:
                return resp
            return web.json_response({"error": "upstream MCP server unavailable"}, status=502)

    async def _gate(self, body: bytes):
        """Return a denial payload if the user blocks, else None."""
        try:
            payload = json.loads(body)
        except ValueError:
            return None
        gated = gated_in(payload)
        if not gated:
            return None
        loop = asyncio.get_running_loop()
        cancel = threading.Event()
        try:  # ask for every gated item in the (rare) batch concurrently
            decisions = await asyncio.gather(*(
                loop.run_in_executor(
                    self.pool, request_approval,
                    {"source": self.source, "server": self.server_name, **describe(m)}, cancel)
                for m in gated))
        finally:
            cancel.set()  # also fires if Claude disconnects: popups get dismissed
        denied = next((d for d in decisions if not d.allowed), None)
        if denied is None:
            return None
        log.info("blocked: %s", denied.reason)
        return denial_for(payload, denied.reason)


def serve(proxy: HttpGuardProxy, host: str, port: int) -> None:
    web.run_app(proxy.make_app(), host=host, port=port, handler_cancellation=True,
                print=lambda *_: None)
