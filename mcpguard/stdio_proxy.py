"""stdio man-in-the-middle for Claude Desktop's local MCP servers.

Claude Desktop launches each server from claude_desktop_config.json and talks
newline-delimited JSON-RPC over its stdin/stdout. `mcpguard install` rewrites each
entry so Claude launches *this proxy*, which launches the real server:

    Claude Desktop <-stdio-> StdioGuard <-stdio-> real MCP server

Gated requests (tools/call, resources/read, prompts/get) are held in their own
worker thread until the user decides. Allowed ones are forwarded byte-for-byte;
blocked ones are answered locally and never reach the server. Everything else
passes straight through. Threads (not asyncio) are used because reading a
console stdin asynchronously is unreliable on Windows.
"""
from __future__ import annotations

import json
import logging
import shutil
import subprocess
import sys
import threading

from .client import request_approval
from .protocol import denial_for, describe, gated_in

log = logging.getLogger("mcpguard.stdio")


class StdioGuard:
    def __init__(self, name: str, command: list[str], source: str = "claude-desktop"):
        self.name, self.command, self.source = name, command, source
        self.child: subprocess.Popen | None = None
        self._out_lock = threading.Lock()    # serialises writes to Claude
        self._in_lock = threading.Lock()     # serialises writes to the real server
        self._pending: dict[str, threading.Event] = {}
        self._pending_lock = threading.Lock()

    # -- main loop ----------------------------------------------------------
    def run(self) -> int:
        exe = shutil.which(self.command[0]) or self.command[0]
        try:
            self.child = subprocess.Popen([exe, *self.command[1:]], stdin=subprocess.PIPE,
                                          stdout=subprocess.PIPE)  # stderr inherited -> Claude logs
        except OSError as exc:
            log.error("cannot start MCP server %r: %s", self.command, exc)
            return 127
        out = threading.Thread(target=self._pump_server_output, daemon=True)
        out.start()
        threading.Thread(target=self._pump_client_input, daemon=True).start()
        code = self.child.wait()
        self._cancel_all()
        out.join(timeout=2)  # flush whatever the server printed last
        return code

    def _pump_server_output(self) -> None:
        for line in iter(self.child.stdout.readline, b""):
            self._to_client(line if line.endswith(b"\n") else line + b"\n")

    def _pump_client_input(self) -> None:
        for raw in iter(sys.stdin.buffer.readline, b""):
            try:
                self._on_client_line(raw)
            except Exception:  # noqa: BLE001 - one bad message must not kill the proxy
                log.exception("error handling client message")
        # Claude closed our stdin: it is shutting down. Mirror that to the real server.
        self._cancel_all()
        try:
            self.child.stdin.close()
        except OSError:
            pass
        try:
            self.child.wait(5)
        except subprocess.TimeoutExpired:
            self.child.terminate()

    # -- message handling ---------------------------------------------------
    def _on_client_line(self, raw: bytes) -> None:
        stripped = raw.strip()
        if not stripped:
            return
        try:
            payload = json.loads(stripped)
        except ValueError:
            self._to_child(raw)  # not ours to judge; let the server complain
            return
        if isinstance(payload, dict):
            method = payload.get("method")
            if method == "initialize":
                log.info("[%s] new MCP session initialize", self.name)
            elif method == "notifications/cancelled":
                rid = (payload.get("params") or {}).get("requestId")
                with self._pending_lock:
                    ev = self._pending.get(json.dumps(rid))
                if ev:
                    ev.set()  # user-side cancel: dismiss popup, don't forward the request
        if gated_in(payload):
            threading.Thread(target=self._guard, args=(payload, raw), daemon=True).start()
        else:
            self._to_child(raw)

    def _guard(self, payload, raw: bytes) -> None:
        gated = gated_in(payload)
        cancel = threading.Event()
        keys = [json.dumps(m["id"]) for m in gated]
        with self._pending_lock:
            for k in keys:
                self._pending[k] = cancel
        denied = None
        try:
            for m in gated:
                info = {"source": self.source, "server": self.name, **describe(m)}
                decision = request_approval(info, cancel)
                if not decision.allowed:
                    denied = decision
                    break
        finally:
            with self._pending_lock:
                for k in keys:
                    self._pending.pop(k, None)
        if cancel.is_set():
            log.info("[%s] request cancelled before decision; dropped", self.name)
            return
        if denied is None:
            self._to_child(raw)
        else:
            log.info("[%s] blocked: %s", self.name, denied.reason)
            reply = denial_for(payload, denied.reason)
            self._to_client((json.dumps(reply) + "\n").encode())

    def _cancel_all(self) -> None:
        with self._pending_lock:
            for ev in set(self._pending.values()):
                ev.set()

    # -- I/O ----------------------------------------------------------------
    def _to_client(self, data: bytes) -> None:
        with self._out_lock:
            try:
                sys.stdout.buffer.write(data)
                sys.stdout.buffer.flush()
            except OSError:
                log.error("Claude closed the pipe; cannot deliver message")

    def _to_child(self, raw: bytes) -> None:
        if not raw.endswith(b"\n"):
            raw += b"\n"
        with self._in_lock:
            try:
                self.child.stdin.write(raw)
                self.child.stdin.flush()
            except (OSError, ValueError):
                log.error("MCP server %s is no longer running; message dropped", self.name)
