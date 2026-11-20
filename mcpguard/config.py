"""Paths and the daemon discovery file.

The daemon writes ``daemon.json`` (port, auth token, timeout) with 0600
permissions. Proxies read it on every request, so a restarted daemon is picked up
without restarting Claude. Resolved lazily so tests can override MCPGUARD_HOME.
"""
from __future__ import annotations

import json
import os
import secrets
from pathlib import Path

DEFAULT_PORT = 47653
DEFAULT_TIMEOUT = 60.0


def home() -> Path:
    return Path(os.environ.get("MCPGUARD_HOME") or Path.home() / ".mcpguard")


def info_file() -> Path:
    return home() / "daemon.json"


def audit_file() -> Path:
    return home() / "audit.log"


def new_token() -> str:
    return secrets.token_urlsafe(32)


def write_daemon_info(port: int, token: str, timeout: float) -> None:
    home().mkdir(parents=True, exist_ok=True)
    tmp = info_file().with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump({"port": port, "token": token, "timeout": timeout}, f)
    os.replace(tmp, info_file())


def read_daemon_info() -> dict:
    with open(info_file()) as f:
        data = json.load(f)
    return {"port": int(data["port"]), "token": str(data["token"]),
            "timeout": float(data.get("timeout", DEFAULT_TIMEOUT))}


def remove_daemon_info() -> None:
    try:
        info_file().unlink()
    except FileNotFoundError:
        pass
