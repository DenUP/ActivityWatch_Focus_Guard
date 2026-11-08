"""Persistent, intentionally non-cancellable study-session deadlines.

No ActivityWatch data or browsing history is stored here.  A fixed UTC deadline
survives closing and reopening the desktop application.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


ALLOWED_MINUTES = (25, 40, 60)
BRIDGE_HOST = "127.0.0.1"
BRIDGE_PORT = 8765


def session_path() -> Path:
    root = os.getenv("LOCALAPPDATA")
    folder = Path(root) if root else Path.home() / ".local" / "share"
    return folder / "ActivityWatchFocusGuard" / "study_session.json"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class StudySession:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.RLock()

    def _read(self) -> dict[str, Any]:
        try:
            content = json.loads(self.path.read_text(encoding="utf-8"))
            deadline = datetime.fromisoformat(content["ends_at"].replace("Z", "+00:00"))
            if deadline.tzinfo is None:
                return {}
            return {"ends_at": deadline.astimezone(timezone.utc),
                    "minutes": content.get("minutes")}
        except (OSError, ValueError, TypeError, KeyError):
            return {}

    def status(self, now: datetime | None = None) -> dict[str, Any]:
        with self._lock:
            current = now or utc_now()
            content = self._read()
            ends_at = content.get("ends_at")
            active = isinstance(ends_at, datetime) and ends_at > current
            return {
                "active": active,
                "ends_at": ends_at.isoformat().replace("+00:00", "Z") if active else None,
                "remaining_seconds": max(0, int((ends_at - current).total_seconds())) if active else 0,
                "minutes": content.get("minutes") if active else None,
            }

    def start(self, minutes: int, now: datetime | None = None) -> dict[str, Any]:
        if type(minutes) is not int or minutes not in ALLOWED_MINUTES:
            raise ValueError("Study duration must be 25, 40, or 60 minutes")
        with self._lock:
            current = now or utc_now()
            if self.status(current)["active"]:
                raise ValueError("Study session is already active; wait for its deadline")
            deadline = current + timedelta(minutes=minutes)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(".tmp")
            temporary.write_text(
                json.dumps({"ends_at": deadline.isoformat().replace("+00:00", "Z"),
                            "minutes": minutes}), encoding="utf-8"
            )
            temporary.replace(self.path)
            return self.status(current)


def create_status_server(session: StudySession, host: str = BRIDGE_HOST,
                         port: int = BRIDGE_PORT) -> ThreadingHTTPServer:
    """Read-only API for the Chrome/Edge extension; bound to loopback only."""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if urlsplit(self.path).path != "/status":
                self.send_error(404)
                return
            result = json.dumps(session.status()).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Length", str(len(result)))
            self.end_headers()
            self.wfile.write(result)

        def log_message(self, _format: str, *args: Any) -> None:
            pass

    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    return server
