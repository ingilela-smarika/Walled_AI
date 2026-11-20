"""Tkinter popup UI. Tk must live on the main thread; the daemon talks to it via a queue.

Each pending request gets its own always-on-top window (stacked with an offset).
Safety details: focus starts on Block, Allow is disabled for the first 700 ms so a
stray keystroke can't approve something, Esc / window-close = Block, Enter does nothing.
"""
from __future__ import annotations

import json
import queue
import time
import tkinter as tk
from tkinter import scrolledtext, ttk
from typing import Callable

MAX_ARGS_CHARS = 6000


class TkApprovalUI:
    def __init__(self) -> None:
        self.root = tk.Tk()
        self.root.withdraw()
        self._q: queue.Queue = queue.Queue()
        self._popups: dict[int, tk.Toplevel] = {}
        self.root.after(100, self._poll)

    # -- thread-safe API used by the daemon ---------------------------------
    def show(self, ticket, request, deadline, on_decision) -> None:
        self._q.put(("show", ticket, request, deadline, on_decision))

    def dismiss(self, ticket) -> None:
        self._q.put(("dismiss", ticket))

    def stop(self) -> None:
        self._q.put(("quit",))

    def run(self) -> None:
        self.root.mainloop()

    # -- main-thread internals ----------------------------------------------
    def _poll(self) -> None:
        try:
            while True:
                item = self._q.get_nowait()
                if item[0] == "show":
                    self._open(*item[1:])
                elif item[0] == "dismiss":
                    top = self._popups.pop(item[1], None)
                    if top is not None:
                        top.destroy()
                elif item[0] == "quit":
                    self.root.quit()
                    return
        except queue.Empty:
            pass
        self.root.after(100, self._poll)

    def _open(self, ticket: int, req: dict, deadline: float,
              on_decision: Callable[[bool], None]) -> None:
        top = tk.Toplevel(self.root)
        self._popups[ticket] = top
        top.title("MCPGuard - approval required")
        n = len(self._popups) - 1
        x = (top.winfo_screenwidth() - 580) // 2 + 30 * n
        y = (top.winfo_screenheight() - 460) // 3 + 30 * n
        top.geometry(f"580x460+{x}+{y}")
        top.attributes("-topmost", True)
        top.columnconfigure(1, weight=1)
        top.rowconfigure(5, weight=1)
        finished = {"v": False}

        def decide(allowed: bool) -> None:
            if finished["v"]:
                return
            finished["v"] = True
            self._popups.pop(ticket, None)
            on_decision(allowed)
            top.destroy()

        ttk.Label(top, text="An MCP request is waiting for your approval",
                  font=("TkDefaultFont", 12, "bold")).grid(
            row=0, column=0, columnspan=2, sticky="w", padx=12, pady=(12, 8))
        rows = [("Source", req.get("source")), ("MCP server", req.get("server")),
                ("Action", req.get("method")), ("Tool / resource", req.get("target"))]
        for i, (label, value) in enumerate(rows, start=1):
            ttk.Label(top, text=label + ":").grid(row=i, column=0, sticky="nw", padx=(12, 6))
            ttk.Label(top, text=str(value), wraplength=400).grid(row=i, column=1, sticky="w")
        ttk.Label(top, text="Parameters:").grid(row=5, column=0, sticky="nw", padx=(12, 6))
        box = scrolledtext.ScrolledText(top, height=12, wrap="word")
        text = json.dumps(req.get("arguments"), indent=2, default=str, ensure_ascii=False)
        if len(text) > MAX_ARGS_CHARS:
            text = text[:MAX_ARGS_CHARS] + f"\n... [{len(text) - MAX_ARGS_CHARS} more chars truncated]"
        box.insert("1.0", text)
        box.configure(state="disabled")
        box.grid(row=5, column=1, sticky="nsew", padx=(0, 12), pady=6)

        countdown = ttk.Label(top, foreground="gray")
        countdown.grid(row=6, column=0, columnspan=2, sticky="w", padx=12)
        bar = ttk.Frame(top)
        bar.grid(row=7, column=0, columnspan=2, sticky="e", padx=12, pady=12)
        block = ttk.Button(bar, text="Block", command=lambda: decide(False))
        allow = ttk.Button(bar, text="Allow", command=lambda: decide(True), state="disabled")
        block.pack(side="left", padx=6)
        allow.pack(side="left")
        top.after(700, lambda: allow.winfo_exists() and allow.configure(state="normal"))
        top.protocol("WM_DELETE_WINDOW", lambda: decide(False))
        top.bind("<Escape>", lambda e: decide(False))

        def tick() -> None:
            if finished["v"] or not top.winfo_exists():
                return
            left = max(0, int(deadline - time.time()))
            countdown.configure(text=f"Auto-blocks in {left}s if you don't respond")
            top.attributes("-topmost", True)  # keep above other windows
            top.after(500, tick)

        tick()
        top.lift()
        top.focus_force()
        block.focus_set()
        top.bell()
