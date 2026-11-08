import unittest
import queue
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from focus_guard import FocusGuard, FocusTracker, category_for_url, classify, event_is_live
from study_session import StudySession
from system_block import (END_MARKER, START_MARKER, WindowsHostsBlocker,
                          block_content, remove_managed_content,
                          restore_powershell_script, start_system_session)


class DetectionTests(unittest.TestCase):
    def test_domains(self):
        self.assertEqual(category_for_url("https://www.youtube.com/shorts/xyz"), "shorts")
        self.assertEqual(category_for_url("https://youtube.com/watch?v=x"), "youtube")
        self.assertEqual(category_for_url("https://www.instagram.com/reels/123"), "reels")
        self.assertIsNone(category_for_url("https://youtube.com.evil.example/shorts/xyz"))
        self.assertIsNone(category_for_url("https://github.com/ActivityWatch"))

    def test_foreground_url(self):
        self.assertEqual(classify({"app": "chrome.exe", "url": "https://youtube.com/shorts/a"},
                                  None, []), "shorts")

    def test_web_bucket_requires_matching_active_title(self):
        window = {"app": "chrome.exe", "title": "GitHub - Google Chrome"}
        web = {"url": "https://youtube.com/watch?v=a", "title": "Music on YouTube"}
        self.assertIsNone(classify(window, web, []))
        window["title"] = "Music on YouTube - Google Chrome"
        self.assertEqual(classify(window, web, []), "youtube")

    def test_ignore_foreground_app_without_browser(self):
        self.assertIsNone(classify({"app": "Code.exe", "title": "YouTube client"}, None, []))

    def test_stale_events(self):
        now = datetime(2026, 11, 8, 16, 0, tzinfo=timezone.utc)
        self.assertFalse(event_is_live({"timestamp": "2026-11-08T15:00:00Z",
                                        "duration": 120, "data": {}}, now, 30))
        self.assertTrue(event_is_live({"timestamp": "2026-11-08T15:59:45Z",
                                       "duration": 10, "data": {}}, now, 30))

    def test_snooze(self):
        tracker = FocusTracker({"youtube": 60})
        self.assertFalse(tracker.observe("youtube", 100))
        self.assertFalse(tracker.observe("youtube", 159))
        self.assertTrue(tracker.observe("youtube", 160))
        tracker.snooze("youtube", 100, 160)
        self.assertFalse(tracker.observe("youtube", 250))
        self.assertTrue(tracker.observe("youtube", 261))
        self.assertFalse(tracker.observe(None, 262))
        self.assertFalse(tracker.observe("youtube", 263))


class TrayBehaviorTests(unittest.TestCase):
    def test_close_hides_window_when_tray_is_ready(self):
        root = SimpleNamespace(withdraw=Mock(), iconify=Mock())
        fake = SimpleNamespace(root=root, tray_icon=SimpleNamespace(visible=True))
        FocusGuard.hide_to_tray(fake)
        root.withdraw.assert_called_once()
        root.iconify.assert_not_called()

    def test_icon_initialization_never_traps_an_invisible_app(self):
        root = SimpleNamespace(withdraw=Mock(), iconify=Mock())
        fake = SimpleNamespace(root=root, tray_icon=SimpleNamespace(visible=False))
        FocusGuard.hide_to_tray(fake)
        root.iconify.assert_called_once()
        root.withdraw.assert_not_called()

    def test_tray_menu_is_processed_on_ui_loop(self):
        commands = queue.SimpleQueue()
        commands.put("show")
        fake = SimpleNamespace(
            tray_commands=commands, show_main_window=Mock(), shutdown=Mock(),
            process_tray_commands=Mock(),
            root=SimpleNamespace(after=Mock())
        )
        FocusGuard.process_tray_commands(fake)
        fake.show_main_window.assert_called_once()
        fake.root.after.assert_called_once()


class StudySessionTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / "session.json"
        self.session = StudySession(self.path)
        self.now = datetime(2026, 11, 8, 15, 0, tzinfo=timezone.utc)

    def test_starts_a_fixed_25_minute_deadline_and_survives_restart(self):
        started = self.session.start(25, self.now)
        self.assertTrue(started["active"])
        self.assertEqual(started["minutes"], 25)
        new_instance = StudySession(self.path)
        middle = new_instance.status(self.now + timedelta(minutes=24))
        self.assertEqual(middle["remaining_seconds"], 60)
        self.assertFalse(new_instance.status(self.now + timedelta(minutes=25))["active"])

    def test_minimize_and_repeated_start_cannot_reset_the_clock(self):
        first = self.session.start(40, self.now)
        with self.assertRaises(ValueError):
            self.session.start(25, self.now + timedelta(minutes=2))
        second = self.session.status(self.now + timedelta(minutes=2))
        self.assertEqual(second["ends_at"], first["ends_at"])
        self.assertEqual(second["remaining_seconds"], 38 * 60)

    def test_only_approved_durations(self):
        for duration in (0, 1, 15, 120, True, "25"):
            with self.assertRaises(ValueError):
                self.session.start(duration, self.now)

    def test_bad_session_data_does_not_crash(self):
        self.path.write_text("{invalid!", encoding="utf-8")
        self.assertFalse(self.session.status(self.now)["active"])



class SystemBlockTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.hosts_path = Path(self.folder.name) / "hosts"
        self.original = b"127.0.0.1 localhost\r\n\x80 user entry\r\n"
        self.hosts_path.write_bytes(self.original)
        self.blocker = WindowsHostsBlocker(self.hosts_path)

    def test_hosts_are_byte_for_byte_restored(self):
        self.blocker.apply()
        self.assertTrue(self.blocker.is_blocked())
        self.assertIn(b"0.0.0.0 www.youtube.com", self.hosts_path.read_bytes())
        self.blocker.restore()
        self.assertEqual(self.hosts_path.read_bytes(), self.original)

    def test_hosts_without_trailing_newline_are_preserved(self):
        original = b"127.0.0.1 localhost"
        self.hosts_path.write_bytes(original)
        self.blocker.apply()
        self.blocker.restore()
        self.assertEqual(self.hosts_path.read_bytes(), original)

    def test_other_program_edits_are_retained(self):
        self.blocker.apply()
        self.hosts_path.write_bytes(self.hosts_path.read_bytes() + b"10.0.0.1 other.app\r\n")
        self.blocker.restore()
        self.assertEqual(self.hosts_path.read_bytes(), self.original + b"10.0.0.1 other.app\r\n")

    def test_corrupt_managed_region_requires_manual_recovery(self):
        with self.assertRaises(ValueError):
            remove_managed_content(self.original + START_MARKER + b"0.0.0.0 youtube.com\r\n")
        with self.assertRaises(ValueError):
            remove_managed_content(self.original + END_MARKER)
        with self.assertRaises(ValueError):
            remove_managed_content(self.original + block_content() + block_content())

    def test_scheduled_task_contains_standalone_restore_code(self):
        script = restore_powershell_script()
        self.assertIn("System.IO.File", script)
        self.assertIn("ActivityWatchFocusGuard managed block", script)
        self.assertIn("28591", script)  # Lossless round-trip of arbitrary bytes.

    @patch("system_block.is_admin", return_value=True)
    def test_scheduler_failure_leaves_hosts_untouched(self, _admin):
        session = StudySession(Path(self.folder.name) / "session.json")
        self.blocker.schedule_restore = Mock(side_effect=RuntimeError("Tasks disabled"))
        with self.assertRaisesRegex(RuntimeError, "Tasks disabled"):
            start_system_session(session, 25, self.blocker)
        self.assertEqual(self.hosts_path.read_bytes(), self.original)
        self.assertFalse(session.status()["active"])

    @patch("system_block.is_admin", return_value=True)
    def test_success_schedules_rollback_before_modifying_hosts(self, _admin):
        session = StudySession(Path(self.folder.name) / "session.json")
        operations = []
        self.blocker.schedule_restore = Mock(side_effect=lambda _: operations.append("schedule"))
        real_apply = self.blocker.apply

        def apply():
            operations.append("apply")
            real_apply()

        self.blocker.apply = apply
        start_system_session(session, 40, self.blocker)
        self.assertEqual(operations, ["schedule", "apply"])
        self.assertTrue(self.blocker.is_blocked())
        self.assertTrue(session.status()["active"])


if __name__ == "__main__":
    unittest.main()
