"""Minimal stdio MCP server used by the tests. Records executed tool calls."""
import json, sys, os

for line in sys.stdin:
    msg = json.loads(line)
    if "id" not in msg:
        continue
    if msg["method"] == "tools/call":
        with open(os.environ["FAKE_LOG"], "a") as f:
            f.write(msg["params"]["name"] + "\n")
        result = {"content": [{"type": "text", "text": "ran:" + msg["params"]["name"]}]}
    else:
        result = {}
    print(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": result}), flush=True)
