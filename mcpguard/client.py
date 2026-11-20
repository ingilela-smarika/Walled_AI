"""Blocking client the proxies use to ask the daemon for a decision.

Fails closed: if the daemon is down, slow, or misbehaves, the answer is "block".
Closing the connection (e.g. when the requester cancels) makes the daemon dismiss
the popup, so no orphaned dialogs are left behind.
"""
from __future__ import annotations

import json
import socket
import threading
import time
from dataclasses import dataclass

from . import config

GRACE = 5.0  # extra seconds beyond the daemon's own timeout


@dataclass
class Decision:
    allowed: bool
    reason: str


def request_approval(info: dict, cancel: threading.Event | None = None) -> Decision:
    try:
        daemon = config.read_daemon_info()
    except (OSError, ValueError, KeyError):
        return Decision(False, "the approval service is not running")
    payload = dict(info, token=daemon["token"])
    deadline = time.monotonic() + daemon["timeout"] + GRACE
    try:
        with socket.create_connection(("127.0.0.1", daemon["port"]), timeout=3) as s:
            s.sendall((json.dumps(payload) + "\n").encode())
            s.settimeout(0.25)
            buf = b""
            while b"\n" not in buf:
                if cancel is not None and cancel.is_set():
                    return Decision(False, "request cancelled")
                if time.monotonic() > deadline:
                    return Decision(False, "approval timed out")
                try:
                    chunk = s.recv(65536)
                except socket.timeout:
                    continue
                if not chunk:
                    return Decision(False, "the approval service closed the connection")
                buf += chunk
        reply = json.loads(buf.split(b"\n", 1)[0])
        return Decision(reply.get("decision") == "allow", str(reply.get("reason", "")))
    except (OSError, ValueError) as exc:
        return Decision(False, f"approval service unavailable ({exc})")
