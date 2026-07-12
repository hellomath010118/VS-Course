#!/usr/bin/env python3
"""One-click launcher for the ASC timetable web app.

Serves this folder over http://localhost and opens the timetable in your
browser. This is the reliable way to run the app fully offline: a plain
``file://`` double-click can't load the vendored Pyodide runtime (browsers
block the local ES-module fetch), but serving over ``http://`` works with the
local ``./pyodide/`` and makes zero external network requests.

Usage::

    python3 serve.py
    ./serve.py                   # if executable (or double-click -> "run")
"""
from __future__ import annotations

import contextlib
import socketserver
import threading
import webbrowser
from functools import partial
from http.server import SimpleHTTPRequestHandler
from pathlib import Path

PAGE = "timetable.html"
PREFERRED_PORT = 8000
WEBROOT = Path(__file__).resolve().parent


class _QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *args) -> None:  # silence per-request logging
        pass


def _make_server() -> socketserver.TCPServer:
    socketserver.TCPServer.allow_reuse_address = True
    handler = partial(_QuietHandler, directory=str(WEBROOT))
    for port in (PREFERRED_PORT, 0):  # friendly port first, else any free one
        try:
            return socketserver.TCPServer(("127.0.0.1", port), handler)
        except OSError:
            continue
    raise SystemExit("Could not bind a local port.")


def main() -> None:
    if not (WEBROOT / PAGE).exists():
        raise SystemExit(
            f"{PAGE} not found in {WEBROOT}.\n"
            f"Build it first:  python3 asc_parser.py --build"
        )
    httpd = _make_server()
    port = httpd.server_address[1]
    url = f"http://localhost:{port}/{PAGE}"
    print(f"Serving {WEBROOT} at {url}")
    print("Fully offline (uses the vendored ./pyodide/). Press Ctrl+C to stop.")
    threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    with httpd, contextlib.suppress(KeyboardInterrupt):
        httpd.serve_forever()
    print("\nStopped.")


if __name__ == "__main__":
    main()
