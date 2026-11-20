"""Approval daemon: localhost TCP service that turns requests into popups.

Wire protocol (newline-delimited JSON, one request per connection):
  -> {"token", "source", "server", "method", "target", "arguments"}
  <- {"decision": "allow"|"block", "reason": "..."}
Every pending request is its own asyncio task, so any number can wait concurrently.
Each gets its own popup. The decision timeout is enforced here (fail closed).
"""
from __future__ import annotations

import asyncio
import hmac
import itertools
import json
import logging
import threading
import time
from typing import Callable, Protocol

from . import config

log = logging.getLogger("mcpguard.daemon")


class ApprovalUI(Protocol):
    def show(self, ticket: int, request: dict, deadline: float,
             on_decision: Callable[[bool], None]) -> None: ...
    def dismiss(self, ticket: int) -> None: ...


class HeadlessUI:
    """Auto-decides. For tests/CI only; never use as a real security control."""

    def __init__(self, allow: bool):
        self.allow = allow
        self._stop = threading.Event()

    def show(self, ticket, request, deadline, on_decision):
        on_decision(self.allow)

    def dismiss(self, ticket):
        pass

    def run(self):
        self._stop.wait()

    def stop(self):
        self._stop.set()


_FIELDS = ("source", "server", "method", "target", "arguments")


class ApprovalDaemon:
    def __init__(self, ui: ApprovalUI, timeout: float = config.DEFAULT_TIMEOUT,
                 port: int = config.DEFAULT_PORT):
        self.ui, self.timeout, self._requested_port = ui, timeout, port
        self.port = 0
        self.token = config.new_token()
        self._ids = itertools.count(1)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stopped: asyncio.Event | None = None
        self._error: BaseException | None = None

    # -- lifecycle ----------------------------------------------------------
    def start(self) -> int:
        self._loop = asyncio.new_event_loop()
        ready = threading.Event()

        def runner():
            asyncio.set_event_loop(self._loop)
            try:
                self._loop.run_until_complete(self._serve(ready))
            except BaseException as exc:  # noqa: BLE001 - surfaced to start()
                self._error = exc
                ready.set()

        threading.Thread(target=runner, daemon=True, name="mcpguard-daemon").start()
        ready.wait(10)
        if self._error:
            raise self._error
        return self.port

    def stop(self) -> None:
        config.remove_daemon_info()
        if self._loop and self._stopped:
            self._loop.call_soon_threadsafe(self._stopped.set)

    async def _serve(self, ready: threading.Event) -> None:
        self._stopped = asyncio.Event()
        server = await asyncio.start_server(self._handle, "127.0.0.1", self._requested_port,
                                            limit=4 * 1024 * 1024)
        self.port = server.sockets[0].getsockname()[1]
        config.write_daemon_info(self.port, self.token, self.timeout)
        log.info("listening on 127.0.0.1:%d", self.port)
        ready.set()
        await self._stopped.wait()
        server.close()
        await server.wait_closed()

    # -- request handling ---------------------------------------------------
    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        ticket, shown = next(self._ids), False
        loop = asyncio.get_running_loop()
        try:
            req = json.loads(await asyncio.wait_for(reader.readline(), 10))
            if not isinstance(req, dict) or not hmac.compare_digest(
                    str(req.get("token", "")), self.token):
                await self._reply(writer, False, "unauthorized")
                return
            info = {k: req.get(k) for k in _FIELDS}
            fut: asyncio.Future = loop.create_future()

            def on_decision(allowed: bool) -> None:
                loop.call_soon_threadsafe(lambda: fut.done() or fut.set_result(allowed))

            self.ui.show(ticket, info, time.time() + self.timeout, on_decision)
            shown = True
            gone = asyncio.ensure_future(reader.read(1))  # EOF => requester went away
            done, _ = await asyncio.wait({fut, gone}, timeout=self.timeout,
                                         return_when=asyncio.FIRST_COMPLETED)
            gone.cancel()
            if fut in done:
                allowed = fut.result()
                reason = "allowed by user" if allowed else "blocked by user"
            elif gone in done:
                self._audit(info, "abandoned", "requester disconnected")
                return
            else:
                allowed, reason = False, f"no response within {self.timeout:.0f}s"
            self._audit(info, "allow" if allowed else "block", reason)
            await self._reply(writer, allowed, reason)
        except (asyncio.TimeoutError, ValueError, ConnectionError):
            pass  # malformed/stalled client: drop it, it will fail closed
        except Exception:  # noqa: BLE001
            log.exception("unexpected error handling request")
        finally:
            if shown:
                self.ui.dismiss(ticket)
            writer.close()

    @staticmethod
    async def _reply(writer: asyncio.StreamWriter, allowed: bool, reason: str) -> None:
        writer.write((json.dumps({"decision": "allow" if allowed else "block",
                                  "reason": reason}) + "\n").encode())
        await writer.drain()

    @staticmethod
    def _audit(info: dict, decision: str, reason: str) -> None:
        try:
            config.home().mkdir(parents=True, exist_ok=True)
            entry = dict(info, ts=time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                         decision=decision, reason=reason,
                         arguments=json.dumps(info.get("arguments"), default=str)[:2000])
            with open(config.audit_file(), "a") as f:
                f.write(json.dumps(entry) + "\n")
        except OSError:
            log.warning("could not write audit log")
