"""JSON-RPC / MCP helpers shared by the stdio and HTTP proxies."""
from __future__ import annotations

from typing import Any

# Client->server requests that cause the server to act or disclose data.
# Everything else (initialize, tools/list, ping, notifications...) passes untouched.
GATED_METHODS = frozenset({"tools/call", "resources/read", "prompts/get"})
DENIED_CODE = -32001


def is_gated(msg: Any) -> bool:
    return isinstance(msg, dict) and msg.get("method") in GATED_METHODS and "id" in msg


def gated_in(payload: Any) -> list[dict]:
    msgs = payload if isinstance(payload, list) else [payload]
    return [m for m in msgs if is_gated(m)]


def describe(msg: dict) -> dict:
    """Extract what the user needs to see: method, target (tool/uri) and arguments."""
    params = msg.get("params") or {}
    if not isinstance(params, dict):
        params = {}
    if msg["method"] == "resources/read":
        return {"method": msg["method"], "target": params.get("uri"), "arguments": None}
    return {"method": msg["method"], "target": params.get("name"),
            "arguments": params.get("arguments")}


def _denial(msg: dict, reason: str) -> dict:
    text = f"Request blocked by the user (MCPGuard): {reason}"
    if msg.get("method") == "tools/call":
        # Tool-level failure: the model sees the text and can explain it to the user.
        return {"jsonrpc": "2.0", "id": msg["id"],
                "result": {"content": [{"type": "text", "text": text}], "isError": True}}
    return {"jsonrpc": "2.0", "id": msg["id"], "error": {"code": DENIED_CODE, "message": text}}


def denial_for(payload: Any, reason: str) -> Any:
    """Build the reply for a denied message (or batch: every request in it is denied)."""
    if isinstance(payload, list):
        return [_denial(m, reason) for m in payload
                if isinstance(m, dict) and "id" in m and "method" in m]
    return _denial(payload, reason)
