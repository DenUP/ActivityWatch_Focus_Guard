"""A privacy-first Windows intervention tool for ActivityWatch 0.14.x.

Read-only ActivityWatch API access. The only automatic intervention is a
reversible, user-dismissible attention prompt. No telemetry or reports.
"""

from __future__ import annotations

import ctypes
import json
import re
import sys
import time
import tkinter as tk
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tkinter import ttk
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen


DEFAULT_CONFIG = {
    "activitywatch_url": "http://127.0.0.1:5600",
    "poll_seconds": 5,
    "event_grace_seconds": 30,
    "delay_to_continue_seconds": 15,
    "break_minutes": 10,
    "study_video_minutes": 30,
    "after_close_cooldown_seconds": 90,
    "rules": {
        "shorts": {"seconds": 40, "label": "коротких видео"},
        "youtube": {"seconds": 180, "label": "YouTube"},
        "reels": {"seconds": 45, "label": "Reels / TikTok"},
        "social": {"seconds": 180, "label": "социальной сети"},
        "games": {"seconds": 300, "label": "игре"},
    },
    "game_apps": ["steam.exe", "cs2.exe", "dota2.exe"],
}

BROWSER_PARTS = (
    "chrome", "firefox", "msedge", "brave", "opera", "vivaldi", "browser"
)


def console_log(message: str) -> None:
    # In a PyInstaller --windowed exe, stdout may be absent altogether.
    if sys.stdout is not None:
        print(message)


def load_config(path: Path) -> dict[str, Any]:
    config = json.loads(json.dumps(DEFAULT_CONFIG))
    if path.exists():
        supplied = json.loads(path.read_text(encoding="utf-8"))
        unknown = set(supplied) - set(config)
        if unknown:
            raise ValueError(f"Unknown config keys: {', '.join(sorted(unknown))}")
        config.update({key: value for key, value in supplied.items() if key != "rules"})
        if "rules" in supplied:
            for name, values in supplied["rules"].items():
                if name not in config["rules"]:
                    raise ValueError(f"Unknown rule: {name}")
                config["rules"][name].update(values)
    address = urlparse(config["activitywatch_url"])
    if address.scheme != "http" or address.hostname not in {"localhost", "127.0.0.1"}:
        raise ValueError("ActivityWatch address must point to local HTTP on localhost")
    if not (1 <= config["poll_seconds"] <= 60):
        raise ValueError("poll_seconds must be between 1 and 60")
    for rule in config["rules"].values():
        if not isinstance(rule["seconds"], (int, float)) or rule["seconds"] < 5:
            raise ValueError("All rule seconds must be at least 5")
    return config


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def event_is_live(event: dict[str, Any] | None, now: datetime, grace: float) -> bool:
    """An event that recently ended can still be the current heartbeat."""
    if not event:
        return False
    try:
        start = parse_time(event["timestamp"])
        duration = max(0.0, float(event.get("duration", 0)))
    except (KeyError, ValueError, TypeError):
        return False
    end = start + timedelta(seconds=duration)
    return start <= now + timedelta(seconds=5) and end >= now - timedelta(seconds=grace)


def is_browser(app: str) -> bool:
    return any(part in app.lower() for part in BROWSER_PARTS)


def web_event_matches_window(window: dict[str, Any], web: dict[str, Any]) -> bool:
    """Avoid attributing a stale background tab to a different foreground tab."""
    window_title = str(window.get("title", "")).casefold()
    web_title = str(web.get("title", "")).casefold()
    if not window_title or not web_title:
        return False
    cleaned = re.sub(r"\s+", " ", web_title).strip()
    return len(cleaned) >= 5 and cleaned in window_title


def category_for_url(url: str) -> str | None:
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower().removeprefix("www.")
        path = parsed.path.lower()
    except ValueError:
        return None

    if host in {"youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be"}:
        return "shorts" if path.startswith("/shorts") else "youtube"
    if host in {"instagram.com", "tiktok.com"}:
        return "reels" if host == "tiktok.com" or path.startswith("/reels") else "social"
    if host in {"vk.com", "reddit.com", "x.com", "twitter.com"}:
        return "social"
    return None


def classify(window: dict[str, Any], web: dict[str, Any] | None,
             game_apps: list[str]) -> str | None:
    app = str(window.get("app", "")).lower()
    if app in {name.lower() for name in game_apps}:
        return "games"
    if not is_browser(app):
        return None

    # Current window URL is authoritative when available (v0.14 may expose it).
    url = window.get("url")
    if isinstance(url, str) and url:
        return category_for_url(url)

    # A browser watcher is optional, and only used when the tab title agrees.
    if web and web_event_matches_window(window, web):
        url = web.get("url")
        if isinstance(url, str):
            category = category_for_url(url)
            if category:
                return category

    title = str(window.get("title", "")).casefold()
    # Fallback is deliberately conservative: don't guess particular videos.
    if "youtube" in title or "ютуб" in title:
        return "youtube"
    return None


class ActivityWatch:
    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/") + "/api/0"
        self.buckets: dict[str, str | None] = {}
        self.last_discovery = 0.0

    def get_json(self, path: str) -> Any:
        request = Request(self.base_url + path, headers={"Accept": "application/json"})
        with urlopen(request, timeout=3) as response:
            return json.load(response)

    def discover(self, now: float) -> None:
        if self.buckets and now - self.last_discovery < 120:
            return
        metadata = self.get_json("/buckets/")
        # Prefer an explicit watcher id, not an arbitrary bucket of the same type.
        def choose(prefix: str) -> str | None:
            names = [name for name in metadata if name.startswith(prefix)]
            return sorted(names)[0] if names else None

        self.buckets = {
            "window": choose("aw-watcher-window_"),
            "afk": choose("aw-watcher-afk_"),
            "web": choose("aw-watcher-web-"),
        }
        self.last_discovery = now

    def latest(self, kind: str) -> dict[str, Any] | None:
        bucket = self.buckets.get(kind)
        if bucket is None:
            return None
        events = self.get_json(f"/buckets/{quote(bucket, safe='')}/events?limit=1")
        return events[0] if events else None

    def current_category(self, now: datetime, grace: float,
                         game_apps: list[str]) -> str | None:
        self.discover(time.monotonic())
        if not self.buckets.get("window"):
            raise RuntimeError("No aw-watcher-window bucket found")
        window = self.latest("window")
        if not event_is_live(window, now, grace):
            return None
        if self.buckets.get("afk"):
            afk = self.latest("afk")
            if not event_is_live(afk, now, grace):
                return None
            if afk.get("data", {}).get("status") != "not-afk":
                return None
        web = self.latest("web")
        web_data = web.get("data", {}) if event_is_live(web, now, grace) else None
        return classify(window.get("data", {}), web_data, game_apps)


@dataclass
class FocusTracker:
    thresholds: dict[str, float]
    current: str | None = None
    started: float = 0.0
    ignored_until: dict[str, float] | None = None

    def __post_init__(self) -> None:
        self.ignored_until = {}

    def observe(self, category: str | None, now: float) -> bool:
        if category != self.current:
            self.current = category
            self.started = now
        if category is None:
            return False
        if now < self.ignored_until.get(category, 0):
            return False
        return now - self.started >= self.thresholds[category]

    def snooze(self, category: str, seconds: float, now: float) -> None:
        self.ignored_until[category] = now + seconds
        self.started = now

    def elapsed(self, now: float) -> float:
        return max(0, now - self.started)


def minimize_previous_window(hwnd: int | None) -> bool:
    """Only minimize the previously focused window, on explicit user choice."""
    if sys.platform != "win32" or not hwnd:
        return False
    return bool(ctypes.windll.user32.ShowWindow(hwnd, 6))


class FocusGuard:
    def __init__(self, config: dict[str, Any]):
        self.config = config
        self.api = ActivityWatch(config["activitywatch_url"])
        self.tracker = FocusTracker({k: rule["seconds"] for k, rule in config["rules"].items()})
        self.root = tk.Tk()
        self.status_label: ttk.Label | None = None
        if getattr(sys, "frozen", False):
            self.build_status_window()
        else:
            self.root.withdraw()
        self.popup: tk.Toplevel | None = None
        self.popup_category: str | None = None
        self.popup_since = 0.0
        self.prev_hwnd: int | None = None
        self.countdown_label: ttk.Label | None = None
        self.break_button: ttk.Button | None = None
        self.study_button: ttk.Button | None = None
        self.error_reported = False

    def build_status_window(self) -> None:
        self.root.title("ActivityWatch Focus Guard")
        self.root.geometry("440x225")
        self.root.resizable(False, False)
        self.root.protocol("WM_DELETE_WINDOW", self.root.destroy)
        frame = ttk.Frame(self.root, padding=24)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="Focus Guard работает", font=("Segoe UI", 16, "bold")).pack(
            anchor="w", pady=(0, 8)
        )
        self.status_label = ttk.Label(frame, text="Подключаюсь к ActivityWatch…")
        self.status_label.pack(anchor="w", pady=(0, 8))
        ttk.Label(
            frame, text="Окно вмешательства появится автоматически при длительном отвлечении. "
                        "Можно свернуть это окно; программа продолжит работу.",
            wraplength=390, justify="left"
        ).pack(anchor="w", pady=(0, 15))
        buttons = ttk.Frame(frame)
        buttons.pack(fill="x")
        ttk.Button(buttons, text="Проверить окно", command=self.show_demo).pack(side="left")
        ttk.Button(buttons, text="Свернуть", command=self.root.iconify).pack(side="left", padx=12)
        ttk.Button(buttons, text="Завершить", command=self.root.destroy).pack(side="right")

    def show_demo(self) -> None:
        if self.popup is None:
            self.open_prompt("youtube", time.monotonic())

    def run(self) -> None:
        console_log("Focus Guard running. Press Ctrl+C in the console to stop.")
        console_log("Read-only ActivityWatch access; no logs or reports are created.")
        self.root.after(100, self.tick)
        self.root.mainloop()

    def tick(self) -> None:
        try:
            category = self.api.current_category(
                datetime.now(timezone.utc), self.config["event_grace_seconds"],
                self.config["game_apps"]
            )
            self.error_reported = False
            if self.status_label is not None:
                self.status_label.config(text="ActivityWatch подключён · наблюдение включено")
            now = time.monotonic()
            if self.popup is None and self.tracker.observe(category, now):
                self.open_prompt(category, now)
        except (RuntimeError, URLError, HTTPError, ValueError, TimeoutError) as exc:
            if self.status_label is not None:
                self.status_label.config(text="Нет соединения с ActivityWatch · повтор подключения")
            if not self.error_reported:
                console_log(f"ActivityWatch unavailable: {exc}; retrying silently.")
                self.error_reported = True
            self.tracker.observe(None, time.monotonic())
        finally:
            self.root.after(int(self.config["poll_seconds"] * 1000), self.tick)

    def open_prompt(self, category: str, now: float) -> None:
        self.prev_hwnd = (ctypes.windll.user32.GetForegroundWindow()
                          if sys.platform == "win32" else None)
        self.popup_category = category
        self.popup_since = now
        window = tk.Toplevel(self.root)
        self.popup = window
        window.title("Focus Guard — осознанная пауза")
        window.attributes("-topmost", True)
        window.resizable(False, False)
        window.geometry("490x310")
        window.protocol("WM_DELETE_WINDOW", self.dismiss)
        window.bind("<Escape>", lambda _: self.dismiss())
        frame = ttk.Frame(window, padding=24)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="Пауза перед продолжением", font=("Segoe UI", 16, "bold")).pack(pady=(0, 12))
        seconds = int(self.tracker.elapsed(now))
        label = self.config["rules"][category]["label"]
        ttk.Label(frame, text=f"Ты уже примерно {seconds} сек. в {label}.",
                  wraplength=440).pack(pady=(0, 8))
        ttk.Label(frame, text="Это осознанный выбор или автоматическое отвлечение?",
                  wraplength=440).pack(pady=(0, 16))
        ttk.Button(frame, text="Вернуться к делу (свернуть текущее окно)",
                   command=self.back_to_task).pack(fill="x", pady=3)
        self.study_button = ttk.Button(frame, command=self.study_video)
        self.study_button.pack(fill="x", pady=3)
        self.break_button = ttk.Button(frame, command=self.take_break)
        self.break_button.pack(fill="x", pady=3)
        self.countdown_label = ttk.Label(frame, text="")
        self.countdown_label.pack(pady=(8, 0))
        window.after(100, self.update_countdown)
        window.lift()
        window.focus_force()

    def update_countdown(self) -> None:
        if self.popup is None or self.countdown_label is None:
            return
        seconds_left = max(0, self.config["delay_to_continue_seconds"] -
                           int(time.monotonic() - self.popup_since))
        state = "disabled" if seconds_left else "normal"
        self.break_button.config(state=state,
                                 text=f"Осознанный перерыв ({self.config['break_minutes']} мин)")
        self.study_button.config(state=state,
                                 text=f"Это учебное видео (разрешить {self.config['study_video_minutes']} мин)")
        self.countdown_label.config(
            text=f"Продолжить можно через {seconds_left} сек.  •  Esc — пропустить окно"
            if seconds_left else "Теперь можно выбрать продолжение"
        )
        if seconds_left:
            self.popup.after(250, self.update_countdown)

    def close_prompt(self, snooze_seconds: float) -> None:
        category = self.popup_category
        if category:
            self.tracker.snooze(category, snooze_seconds, time.monotonic())
        if self.popup is not None:
            self.popup.destroy()
        self.popup = None
        self.popup_category = None
        self.prev_hwnd = None

    def back_to_task(self) -> None:
        hwnd = self.prev_hwnd
        self.close_prompt(self.config["after_close_cooldown_seconds"])
        minimize_previous_window(hwnd)

    def study_video(self) -> None:
        self.close_prompt(self.config["study_video_minutes"] * 60)

    def take_break(self) -> None:
        self.close_prompt(self.config["break_minutes"] * 60)

    def dismiss(self) -> None:
        self.close_prompt(self.config["after_close_cooldown_seconds"])


if __name__ == "__main__":
    try:
        frozen = getattr(sys, "frozen", False)
        config_folder = Path(sys.executable).resolve().parent if frozen else Path(__file__).resolve().parent
        options = load_config(config_folder / "config.json")
        if sys.platform == "win32" and frozen:
            # Prevent two background copies running after repeated double-clicks.
            mutex = ctypes.windll.kernel32.CreateMutexW(None, True, "Local\\ActivityWatchFocusGuard")
            if ctypes.windll.kernel32.GetLastError() == 183:
                ctypes.windll.user32.MessageBoxW(
                    None, "Focus Guard уже запущен.", "ActivityWatch Focus Guard", 0x40
                )
                sys.exit(0)
        app = FocusGuard(options)
        if "--demo" in sys.argv:
            console_log("Demo mode: ActivityWatch data are not accessed.")
            app.root.after(100, lambda: app.open_prompt("youtube", time.monotonic()))
            app.root.mainloop()
        else:
            app.run()
    except (ValueError, json.JSONDecodeError, tk.TclError) as error:
        if getattr(sys, "frozen", False) and sys.platform == "win32":
            ctypes.windll.user32.MessageBoxW(None, f"Ошибка запуска Focus Guard: {error}",
                                               "ActivityWatch Focus Guard", 0x10)
        elif sys.stderr is not None:
            print(f"Cannot start Focus Guard: {error}", file=sys.stderr)
        sys.exit(1)
