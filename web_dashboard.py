"""Loopback-only browser dashboard for Focus Guard.

The local browser receives only anonymous session state. Mutating requests
require a per-process capability token supplied in the tray-opened URL fragment,
an exact local Origin, and JSON. No CORS or external network API is enabled.
"""

from __future__ import annotations

import json
import secrets
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.error import URLError
from urllib.parse import urlsplit
from urllib.request import urlopen

from study_session import ALLOWED_MINUTES, StudySession
from system_block import (WindowsHostsBlocker, elevated_operation_mutex,
                          is_admin, restore_system_access, start_system_session)


HOST = "127.0.0.1"
PORT = 8766
ASSETS = {"/": ("index.html", "text/html; charset=utf-8"),
          "/app.css": ("app.css", "text/css; charset=utf-8"),
          "/app.js": ("app.js", "text/javascript; charset=utf-8"),
          "/favicon.svg": ("favicon.svg", "image/svg+xml")}


class FocusService:
    def __init__(self, session: StudySession, blocker: WindowsHostsBlocker,
                 launch_elevated: Callable[..., None],
                 activitywatch_url: str = "http://127.0.0.1:5600"):
        self.session = session
        self.blocker = blocker
        self.launch_elevated = launch_elevated
        self.activitywatch_url = activitywatch_url.rstrip("/")
        self.access_token = secrets.token_urlsafe(32)
        self._aw_checked_at = 0.0
        self._aw_connected = False
        self._aw_lock = threading.Lock()
        self._pending_until = 0.0
        self._action_lock = threading.Lock()

    def _activitywatch_connected(self) -> bool:
        with self._aw_lock:
            if time.monotonic() - self._aw_checked_at > 8:
                try:
                    with urlopen(self.activitywatch_url + "/api/0/info", timeout=0.7) as response:
                        self._aw_connected = response.status == 200
                except (URLError, OSError, TimeoutError):
                    self._aw_connected = False
                self._aw_checked_at = time.monotonic()
            return self._aw_connected

    def status(self) -> dict[str, Any]:
        session = self.session.status()
        try:
            blocked = self.blocker.is_blocked()
            hosts_error = False
        except (OSError, ValueError):
            blocked = False
            hosts_error = True
        if session["active"] and blocked:
            self._pending_until = 0.0
        if hosts_error:
            state = "error"
        elif session["active"] and blocked:
            state = "focus"
        elif session["active"]:
            state = "warning"
        elif blocked:
            state = "stale"
        elif time.monotonic() < self._pending_until:
            state = "pending"
        else:
            state = "ready"
        return {"state": state, "active": session["active"], "blocked": blocked,
                "remaining_seconds": session["remaining_seconds"],
                "ends_at": session["ends_at"], "minutes": session["minutes"],
                "activitywatch_connected": self._activitywatch_connected()}

    def start(self, minutes: int) -> dict[str, Any]:
        with self._action_lock:
            if type(minutes) is not int or minutes not in ALLOWED_MINUTES:
                raise ValueError("Choose 25, 40, or 60 minutes")
            if time.monotonic() < self._pending_until:
                raise ValueError("A Windows permission request is already pending")
            if self.session.status()["active"] or self.blocker.is_blocked():
                raise ValueError("A study session or system block is already active")
            if is_admin():
                with elevated_operation_mutex():
                    start_system_session(self.session, minutes, self.blocker)
                return {"pending": False}
            self.launch_elevated("--system-start", str(minutes))
            self._pending_until = time.monotonic() + 45
            return {"pending": True}

    def restore(self) -> dict[str, Any]:
        with self._action_lock:
            if time.monotonic() < self._pending_until:
                raise ValueError("A Windows permission request is already pending")
            if is_admin():
                with elevated_operation_mutex():
                    restore_system_access(self.session, self.blocker)
                return {"pending": False}
            self.launch_elevated("--system-restore")
            self._pending_until = time.monotonic() + 45
            return {"pending": True}


def make_handler(service: FocusService, asset_root: Path,
                 host: str = HOST, port: int = PORT) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def _allowed_hosts(self) -> set[str]:
            port_number = self.server.server_address[1]
            return {f"{host}:{port_number}", f"localhost:{port_number}"}

        def _send(self, status: int, data: bytes, mime: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'self'; "
                             "style-src 'self'; connect-src 'self'; img-src 'self' data:; "
                             "base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(data)

        def _json(self, status: int, content: dict[str, Any]) -> None:
            self._send(status, json.dumps(content, ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8")

        def _host_is_local(self) -> bool:
            return self.headers.get("Host") in self._allowed_hosts()

        def do_GET(self) -> None:
            if not self._host_is_local():
                self._json(403, {"error": "Invalid host"})
                return
            path = urlsplit(self.path).path
            if path == "/api/status":
                self._json(200, service.status())
                return
            if path not in ASSETS:
                self._json(404, {"error": "Not found"})
                return
            name, mime = ASSETS[path]
            try:
                data = (asset_root / name).read_bytes()
            except OSError:
                self._json(503, {"error": "UI files are missing"})
                return
            self._send(200, data, mime)

        def do_POST(self) -> None:
            if (not self._host_is_local() or
                    self.headers.get("Origin") != f"http://{self.headers.get('Host', '')}" or
                    not self.headers.get("Content-Type", "").lower().startswith("application/json") or
                    not secrets.compare_digest(self.headers.get("X-FocusGuard-Token", ""),
                                               service.access_token)):
                self._json(403, {"error": "Open this dashboard from the Focus Guard tray icon"})
                return
            path = urlsplit(self.path).path
            if path not in {"/api/start", "/api/restore"}:
                self._json(404, {"error": "Not found"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length < 512:
                    raise ValueError("Invalid request length")
                payload = json.loads(self.rfile.read(length))
                if not isinstance(payload, dict):
                    raise ValueError("Expected JSON object")
                if path == "/api/start":
                    if set(payload) != {"minutes"}:
                        raise ValueError("Expected duration")
                    result = service.start(payload["minutes"])
                else:
                    if payload:
                        raise ValueError("Unexpected restore fields")
                    result = service.restore()
                self._json(202 if result["pending"] else 200, {"ok": True, **result})
            except (ValueError, PermissionError, OSError, RuntimeError) as error:
                self._json(400, {"error": str(error)})

        def do_OPTIONS(self) -> None:
            self._json(405, {"error": "CORS is disabled"})

        def log_message(self, _format: str, *args: Any) -> None:
            pass  # No URL/browsing-history logs.

    return Handler


def create_dashboard_server(service: FocusService, asset_root: Path,
                            host: str = HOST, port: int = PORT) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), make_handler(service, asset_root, host, port))
    server.daemon_threads = True
    return server
