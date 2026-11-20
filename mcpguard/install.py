"""Rewrite claude_desktop_config.json so every local MCP server runs via the proxy.

    {"command": "npx", "args": ["-y", "pkg"]}
 -> {"command": "<python>", "args": ["-m", "mcpguard", "stdio", "--name", NAME, "--", "npx", "-y", "pkg"]}

Reversible: `uninstall` unwraps from the text after "--". A one-time backup is kept.
Entries Claude Desktop runs through `mcp-remote` are covered too, since they are stdio.
"""
from __future__ import annotations

import json
import os
import platform
import shutil
import sys
from pathlib import Path


def default_config_path() -> Path:
    system = platform.system()
    if system == "Darwin":
        return Path.home() / "Library/Application Support/Claude/claude_desktop_config.json"
    if system == "Windows":
        return Path(os.environ.get("APPDATA", Path.home())) / "Claude/claude_desktop_config.json"
    return Path.home() / ".config/Claude/claude_desktop_config.json"


def is_wrapped(entry: dict) -> bool:
    args = entry.get("args") or []
    return "mcpguard" in args and "stdio" in args and "--" in args


def wrap(name: str, entry: dict) -> dict:
    new = dict(entry)
    new["command"] = sys.executable
    new["args"] = ["-m", "mcpguard", "stdio", "--name", name, "--",
                   entry["command"], *(entry.get("args") or [])]
    return new


def unwrap(entry: dict) -> dict:
    args = entry["args"]
    cmd = args[args.index("--") + 1:]
    new = dict(entry)
    new["command"], new["args"] = cmd[0], cmd[1:]
    if not new["args"]:
        del new["args"]
    return new


def _load(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _save(path: Path, data: dict) -> None:
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def apply(path: Path, undo: bool = False, dry_run: bool = False) -> list[str]:
    data = _load(path)
    changed = []
    for name, entry in (data.get("mcpServers") or {}).items():
        if not isinstance(entry, dict) or "command" not in entry:
            continue  # remote/url entries are not launched locally
        if undo and is_wrapped(entry):
            data["mcpServers"][name] = unwrap(entry)
            changed.append(name)
        elif not undo and not is_wrapped(entry):
            data["mcpServers"][name] = wrap(name, entry)
            changed.append(name)
    if changed and not dry_run:
        backup = path.with_name(path.name + ".mcpguard.bak")
        if not backup.exists():
            shutil.copy2(path, backup)
        _save(path, data)
    return changed
