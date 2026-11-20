"""Command line entry point: daemon | stdio | http-proxy | install | uninstall."""
from __future__ import annotations

import argparse
import logging
import os
import secrets
import signal
import sys

from . import config


def _daemon(args) -> int:
    from .daemon import ApprovalDaemon, HeadlessUI
    if args.headless:
        ui = HeadlessUI(args.headless == "allow")
    else:
        try:
            from .ui import TkApprovalUI
            ui = TkApprovalUI()
        except Exception as exc:  # noqa: BLE001
            print(f"Cannot start the GUI ({exc}). On Linux install python3-tk.", file=sys.stderr)
            return 1
    port = args.port if args.port is not None else int(os.environ.get("MCPGUARD_PORT",
                                                                       config.DEFAULT_PORT))
    daemon = ApprovalDaemon(ui, timeout=args.timeout, port=port)
    try:
        daemon.start()
    except OSError as exc:
        print(f"Cannot listen on port {port}: {exc}. Is MCPGuard already running?",
              file=sys.stderr)
        return 1
    signal.signal(signal.SIGINT, lambda *_: ui.stop())
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, lambda *_: ui.stop())
    print(f"MCPGuard running on 127.0.0.1:{daemon.port} (timeout {args.timeout:.0f}s). Ctrl+C to quit.")
    try:
        ui.run()
    finally:
        daemon.stop()
    return 0


def _http(args) -> int:
    from .http_proxy import HttpGuardProxy, serve
    secret = None if args.no_secret else (args.path_secret or secrets.token_urlsafe(16))
    proxy = HttpGuardProxy(args.upstream, args.server_name, args.source, secret)
    base = f"http://{args.host}:{args.port}" + (f"/{secret}" if secret else "")
    print(f"Proxying {args.upstream}\nLocal connector URL: {base}/<same path as upstream>\n"
          f"Expose it publicly (e.g. `cloudflared tunnel --url http://localhost:{args.port}`) and "
          f"register <public-url>{'/' + secret if secret else ''}/<path> as the custom connector in Claude.")
    serve(proxy, args.host, args.port)
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    command: list[str] = []
    if "--" in argv:  # everything after -- is the real server command, verbatim
        i = argv.index("--")
        argv, command = argv[:i], argv[i + 1:]

    p = argparse.ArgumentParser(prog="mcpguard")
    sub = p.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("daemon", help="run the approval popup service")
    d.add_argument("--timeout", type=float, default=config.DEFAULT_TIMEOUT)
    d.add_argument("--port", type=int)
    d.add_argument("--headless", choices=["allow", "block"], help="testing only")
    s = sub.add_parser("stdio", help="(internal) wrap one local MCP server: stdio --name N -- cmd args")
    s.add_argument("--name", required=True)
    s.add_argument("--source", default="claude-desktop")
    h = sub.add_parser("http-proxy", help="guard a remote MCP server for Claude Web")
    h.add_argument("--upstream", required=True, help="origin of the real server, e.g. https://mcp.example.com")
    h.add_argument("--server-name")
    h.add_argument("--source", default="claude-web")
    h.add_argument("--host", default="127.0.0.1")
    h.add_argument("--port", type=int, default=8787)
    h.add_argument("--path-secret", help="secret URL prefix (random if omitted)")
    h.add_argument("--no-secret", action="store_true")
    for name in ("install", "uninstall"):
        i = sub.add_parser(name, help=f"{name} the wrapper in claude_desktop_config.json")
        i.add_argument("--config", help="path to claude_desktop_config.json")
        i.add_argument("--dry-run", action="store_true")
    args = p.parse_args(argv)

    if args.cmd == "daemon":
        return _daemon(args)
    if args.cmd == "http-proxy":
        return _http(args)
    if args.cmd == "stdio":
        if not command:
            p.error("stdio needs the server command after --")
        from .stdio_proxy import StdioGuard
        code = StdioGuard(args.name, command, args.source).run()
        sys.stdout.flush()
        os._exit(code)  # don't wait on the blocked stdin reader thread
    from pathlib import Path
    from . import install
    path = Path(args.config) if args.config else install.default_config_path()
    if not path.exists():
        print(f"Config not found: {path}", file=sys.stderr)
        return 1
    changed = install.apply(path, undo=args.cmd == "uninstall", dry_run=args.dry_run)
    verb = "Would change" if args.dry_run else "Changed"
    print(f"{verb} {len(changed)} server(s): {', '.join(changed) or '-'}")
    if changed and not args.dry_run:
        print("Fully quit and restart Claude Desktop to apply.")
    return 0
