"""Persistent, intentionally non-cancellable study-session deadlines.

No ActivityWatch data or browsing history is stored here.  A fixed UTC deadline
survives closing and reopening the desktop application.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import ctypes
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


ALLOWED_MINUTES = (25, 40, 60)


def session_path() -> Path:
    if sys.platform == "win32":
        # Shared location readable by the normal UI and an elevated helper,
        # even if UAC requires credentials for a different admin account.
        buffer = ctypes.create_unicode_buffer(32768)
        status = ctypes.windll.shell32.SHGetFolderPathW(None, 0x23, None, 0, buffer)
        folder = Path(buffer.value) if status == 0 else Path(r"C:\ProgramData")
    else:
        folder = Path(os.getenv("XDG_STATE_HOME", str(Path.home() / ".local" / "state")))
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
