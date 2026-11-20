# MCPGuard

Per-request user approval for MCP traffic from **Claude Desktop** and **Claude Web**.
Every `tools/call`, `resources/read` and `prompts/get` is held until you click **Allow** or **Block** in an always-on-top popup.

```
                          ┌────────────────────────── your machine ───────────────────────────┐
Claude Desktop ─stdio──►  │ StdioGuard (one per server) ──stdio──► real local MCP server       │
                          │        │ ask                                                       │
Claude Web ──HTTPS──► tunnel ─► HttpGuardProxy ──HTTPS──► real remote MCP server              │
   (Anthropic cloud)      │        │ ask                                                       │
                          │        ▼ 127.0.0.1 + token                                         │
                          │   Approval daemon ──► Tk popup (Allow / Block) ──► audit.log       │
                          └────────────────────────────────────────────────────────────────────┘
```

## How interception works (and why it differs per client)

| Client | How it reaches MCP servers | Interception point |
|---|---|---|
| **Claude Desktop**, local servers | Launches each server from `claude_desktop_config.json` and speaks JSON-RPC over stdio | `mcpguard install` rewrites each entry so Claude launches `mcpguard stdio -- <original command>`. The proxy sits on the pipe between Claude and the server. |
| **Claude Desktop** via `mcp-remote` | Same (stdio) | Covered by the same wrapper. |
| **Claude Web** (and remote connectors in Desktop) | Anthropic's *cloud* calls the remote server's URL. Nothing on your machine sees this traffic, so OS-level sniffing cannot work. | Put MCPGuard **in front of the server**: `mcpguard http-proxy` is a reverse proxy; you register the proxy's URL as the custom connector. The popup still appears on your desktop. |

**Request handling**
- Gated methods: `tools/call`, `resources/read`, `prompts/get`. Everything else (`initialize`, `tools/list`, `ping`, notifications, server→client messages, SSE streams) passes through untouched. `initialize` is logged so you can see new sessions start.
- Each gated request runs in its own worker (thread for stdio, task for HTTP) and asks the daemon over a localhost socket. The request stays pending until a decision arrives, so any number run concurrently, each with its own popup. Other traffic is never blocked behind a pending popup.
- **Allow** → original bytes forwarded unchanged. **Block** → the server never sees it; Claude gets a denial: for `tools/call` a result with `isError: true` and text "Request blocked by the user" (so the model can explain it), for other methods JSON-RPC error `-32001`.

## Reliability and security decisions

- **Fail closed.** Daemon down, unreachable, bad reply, or no answer within the timeout (default 60 s) → blocked, with a clear reason.
- **Cancellation.** If Claude sends `notifications/cancelled`, disconnects, or the proxy dies, the popup is dismissed (the daemon notices the socket closing).
- **Shutdown.** Claude closing stdin closes the child's stdin, then terminates it after 5 s. A crashing server ends the proxy with its exit code. stderr is inherited, so server logs still reach Claude's own log files.
- **Daemon IPC is authenticated.** It binds `127.0.0.1` only and requires a random token stored in `~/.mcpguard/daemon.json` (mode 0600), compared in constant time; other local processes without that file can't spoof approvals.
- **Popup anti-misclick.** Focus starts on Block, Allow is disabled for 700 ms, Esc/close = Block, Enter does nothing. Long arguments are truncated for display (full text, capped at 2 KB, in the audit log).
- **Audit log:** `~/.mcpguard/audit.log` (JSON lines: time, source, server, tool, args, decision, reason).
- **HTTP proxy hardening:** optional secret URL prefix (random by default) so strangers hitting your tunnel get 404 and can't spam popups; binds localhost; `Authorization` headers are forwarded but never logged.

## Install

Python ≥ 3.10. Linux also needs Tk: `sudo apt install python3-tk`.

```bash
git clone <this repo> && cd mcpguard
python -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -e .
```

## Run

**1. Start the approval daemon (keep it running):**
```bash
mcpguard daemon            # --timeout 60  --port 47653
```
Auto-start at login: macOS launchd / Windows Task Scheduler ("At log on", run `pythonw -m mcpguard daemon`) / `systemd --user` service.

**2a. Claude Desktop (local servers):**
```bash
mcpguard install --dry-run     # preview
mcpguard install               # backs up to claude_desktop_config.json.mcpguard.bak
```
Fully quit and restart Claude Desktop. Revert any time with `mcpguard uninstall`.
Config path is auto-detected (macOS `~/Library/Application Support/Claude/`, Windows `%APPDATA%\Claude\`, Linux `~/.config/Claude/`); override with `--config`.
Note: the wrapper uses the Python that ran `install`, so keep that virtualenv in place.

**2b. Claude Web (remote servers):**
```bash
mcpguard http-proxy --upstream https://mcp.example.com
# prints a local URL containing a random secret, e.g. http://127.0.0.1:8787/AbC...
cloudflared tunnel --url http://localhost:8787      # or ngrok
```
In claude.ai → Settings → Connectors → *Add custom connector*, use `https://<tunnel-host>/<secret>/<upstream path, e.g. mcp>`.

## Tests
```bash
pip install -e .[dev] && pytest -q
```
Covers: allow, block, 20 concurrent requests, non-gated passthrough, fail-closed when the daemon is down, HTTP proxy allow/block/secret prefix, config install/uninstall round-trip. A headless daemon (`--headless allow|block`) exists only for CI.

## Limitations (honest list)

- **Claude Web can only be guarded for servers you can put a proxy in front of.** Anthropic's first-party connectors and servers you don't control can't be intercepted client-side. Browser-extension approaches are an alternative but outside "Python".
- OAuth-protected upstreams: the OAuth discovery documents live at the connector origin's root and reference the upstream host, so the secret path prefix and host rewriting can break the flow. Use token/API-key auth, or run with `--no-secret` behind an origin where the metadata is consistent.
- The HTTP proxy needs a publicly reachable URL (tunnel) and your machine online; if it is offline, requests simply fail.
- In a JSON-RPC batch, one denial denies the whole batch (batches were removed from newer MCP spec versions).
- Desktop Extensions (`.mcpb`) are not in `claude_desktop_config.json` and aren't wrapped by `install`.
- Always-on-top is best-effort: it works on Windows/Linux; on macOS Tk windows may not appear above another app's full-screen space.
- Server→client requests (e.g. sampling) are not gated.
- Claude Desktop must be restarted after `install`, and updates that rewrite the config would need re-running it.

## Demo script (for the video)
1. `mcpguard daemon`; `mcpguard install`; restart Claude Desktop.
2. Ask Claude to read a file → popup shows server/tool/params → **Allow** → result appears.
3. Ask again → **Block** → Claude reports the tool was blocked; confirm via `~/.mcpguard/audit.log`.
4. Ask for several tools at once → stacked popups, decide each independently.
5. Quit the daemon, ask again → request fails closed.
6. Claude Web: run `http-proxy` + tunnel, add connector, repeat allow/block.

## Layout
`mcpguard/protocol.py` JSON-RPC helpers · `client.py` fail-closed approval client · `daemon.py` IPC + decisions + audit · `ui.py` Tk popup · `stdio_proxy.py` Desktop interception · `http_proxy.py` Web interception · `install.py` config rewriter · `cli.py` entry point.
