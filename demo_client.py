import json, os, subprocess, sys

here = os.path.dirname(os.path.abspath(__file__))
log = os.path.join(here, "executed.log")
if os.path.exists(log):
    os.remove(log)
os.environ["FAKE_LOG"] = log

p = subprocess.Popen(
    [sys.executable, "-m", "mcpguard", "stdio", "--name", "demo-files", "--",
     sys.executable, os.path.join(here, "tests", "fake_server.py")],
    stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)

tools = ["read_file", "delete_file", "send_email"]
for i, name in enumerate(tools, 1):
    msg = {"jsonrpc": "2.0", "id": i, "method": "tools/call",
           "params": {"name": name, "arguments": {"path": "C:/secret.txt"}}}
    p.stdin.write(json.dumps(msg) + "\n")
p.stdin.flush()
print("Sent 3 requests at once. Decide in the popups...")

for _ in tools:
    r = json.loads(p.stdout.readline())["result"]
    status = "BLOCKED" if r.get("isError") else "ALLOWED"
    print(f"id result: {status} -> {r['content'][0]['text']}")

p.stdin.close()
print("Tools that actually executed:", open(log).read().split() if os.path.exists(log) else "none")