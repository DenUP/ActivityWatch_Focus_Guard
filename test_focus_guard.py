import unittest
import json
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from focus_guard import run_app
from study_session import StudySession
from system_block import (END_MARKER, START_MARKER, WindowsHostsBlocker,
                          block_content, extend_system_session, remove_managed_content,
                          restore_powershell_script, start_system_session)
from web_dashboard import FocusService, create_dashboard_server


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

    def test_extension_keeps_a_single_unbroken_deadline(self):
        initial = self.session.start(50, self.now)
        extended = self.session.extend(25, self.now + timedelta(minutes=10))
        self.assertEqual(extended["minutes"], 75)
        self.assertEqual(extended["remaining_seconds"], 65 * 60)
        self.assertNotEqual(extended["ends_at"], initial["ends_at"])
        self.assertEqual(StudySession(self.path).status(self.now + timedelta(minutes=10))["ends_at"],
                         extended["ends_at"])

    def test_extension_rejects_expiry_invalid_durations_and_over_three_hours(self):
        self.session.start(75, self.now)
        for invalid in (True, 10, "25", 60):
            with self.assertRaises(ValueError):
                self.session.extend(invalid, self.now)
        with self.assertRaises(ValueError):
            self.session.extend(25, self.now + timedelta(minutes=75))
        with self.assertRaises(ValueError):
            self.session.extend(25, self.now + timedelta(minutes=74, seconds=45))
        self.session.extend(50, self.now)
        self.session.extend(50, self.now)
        with self.assertRaisesRegex(ValueError, "three hours"):
            self.session.extend(25, self.now)



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

    @patch("system_block.is_admin", return_value=True)
    def test_extend_updates_rollback_before_deadline_without_rewriting_hosts(self, _admin):
        session = StudySession(Path(self.folder.name) / "session.json")
        session.start(50)
        self.blocker.apply()
        original_block = self.hosts_path.read_bytes()
        deadlines = []
        self.blocker.schedule_restore = Mock(side_effect=lambda when: deadlines.append(when))
        self.blocker.apply = Mock(side_effect=AssertionError("Cannot reapply an active block"))
        old_deadline = session.status()["ends_at"]
        result = extend_system_session(session, 25, self.blocker)
        self.assertEqual(result["minutes"], 75)
        self.assertEqual(self.hosts_path.read_bytes(), original_block)
        self.assertEqual(deadlines[0], datetime.fromisoformat(old_deadline.replace("Z", "+00:00")) +
                         timedelta(minutes=25))
        self.blocker.apply.assert_not_called()

    @patch("system_block.is_admin", return_value=True)
    def test_extend_rolls_back_scheduled_deadline_if_state_write_fails(self, _admin):
        session = StudySession(Path(self.folder.name) / "session.json")
        session.start(50)
        self.blocker.apply()
        old_deadline = session.status()["ends_at"]
        self.blocker.schedule_restore = Mock()
        with patch.object(session, "extend", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(OSError, "disk full"):
                extend_system_session(session, 25, self.blocker)
        deadlines = [call.args[0] for call in self.blocker.schedule_restore.call_args_list]
        self.assertEqual(deadlines[1], datetime.fromisoformat(old_deadline.replace("Z", "+00:00")))


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.hosts = Path(self.folder.name) / "hosts"
        self.hosts.write_bytes(b"127.0.0.1 localhost\r\n")
        self.session = StudySession(Path(self.folder.name) / "session.json")
        self.launcher = Mock()
        self.service = FocusService(self.session, WindowsHostsBlocker(self.hosts),
                                    self.launcher, activitywatch_url="http://127.0.0.1:1")
        self.server = create_dashboard_server(
            self.service, Path(__file__).parent / "web", port=0
        )
        self.worker = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.worker.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def json_request(self, path, payload, token=True, origin=True):
        headers = {"Content-Type": "application/json"}
        if token:
            headers["X-FocusGuard-Token"] = self.service.access_token
        if origin:
            headers["Origin"] = self.base
        return Request(self.base + path, data=json.dumps(payload).encode(),
                       headers=headers, method="POST")

    def test_serves_dashboard_from_loopback_without_taskbar_ui(self):
        with urlopen(self.base + "/") as response:
            html = response.read().decode("utf-8")
            self.assertEqual(response.headers["X-Frame-Options"], "DENY")
        self.assertIn("Focus Guard", html)
        self.assertIn("/app.js", html)
        with urlopen(self.base + "/app.css") as response:
            self.assertIn("timer-orbit", response.read().decode())
        with urlopen(self.base + "/api/status") as response:
            self.assertIsNone(json.load(response)["intent"])
        with urlopen(Request(self.base + "/api/status", headers={
                "X-FocusGuard-Token": self.service.access_token})) as response:
            status = json.load(response)
        self.assertEqual(status["state"], "ready")
        self.assertFalse(status["activitywatch_connected"])

    def test_rejects_bad_host_header(self):
        request = Request(self.base + "/", headers={"Host": "bad.example"})
        with self.assertRaises(HTTPError) as ctx:
            urlopen(request)
        self.assertEqual(ctx.exception.code, 403)

    def test_rejects_cross_site_or_missing_token_posts(self):
        for token, origin in ((False, True), (True, False), (False, False)):
            with self.subTest(token=token, origin=origin):
                with self.assertRaises(HTTPError) as ctx:
                    urlopen(self.json_request("/api/start", {"minutes": 25}, token, origin))
                self.assertEqual(ctx.exception.code, 403)
        forged = self.json_request("/api/start", {"minutes": 25})
        forged.add_header("Origin", "https://evil.example")
        with self.assertRaises(HTTPError) as ctx:
            urlopen(forged)
        self.assertEqual(ctx.exception.code, 403)
        self.launcher.assert_not_called()


    @patch("web_dashboard.is_admin", return_value=False)
    def test_valid_study_start_requests_user_controlled_uac(self, _admin):
        with urlopen(self.json_request("/api/start", {"minutes": 25, "intent": "Open the notebook"})) as response:
            self.assertEqual(response.status, 202)
            payload = json.load(response)
        self.assertTrue(payload["pending"])
        self.launcher.assert_called_once_with("--system-start", "25")
        with urlopen(self.base + "/api/status") as response:
            self.assertIsNone(json.load(response)["intent"])
        with urlopen(Request(self.base + "/api/status", headers={
                "X-FocusGuard-Token": self.service.access_token})) as response:
            status = json.load(response)
        self.assertEqual(status["state"], "pending")
        self.assertEqual(status["intent"], "Open the notebook")
        self.assertFalse(self.session.status()["active"])

    @patch("web_dashboard.is_admin", return_value=False)
    def test_extend_requests_uac_without_unlocking_and_keeps_pending(self, _admin):
        self.session.start(50)
        self.service.blocker.apply()
        with urlopen(self.json_request("/api/extend", {"minutes": 25})) as response:
            self.assertEqual(response.status, 202)
        self.launcher.assert_called_once_with("--system-extend", "25")
        with urlopen(self.base + "/api/status") as response:
            status = json.load(response)
        self.assertEqual(status["state"], "focus")
        self.assertEqual(status["pending_action"], "extend")
        with self.assertRaises(HTTPError) as ctx:
            urlopen(self.json_request("/api/extend", {"minutes": 25}))
        self.assertEqual(ctx.exception.code, 400)

    @patch("web_dashboard.is_admin", return_value=False)
    def test_invalid_or_missing_first_step_never_requests_uac(self, _admin):
        payloads = [{"minutes": 25}, {"minutes": 25, "intent": ""},
                    {"minutes": 25, "intent": "ok"},
                    {"minutes": 25, "intent": "Line\nbreak"},
                    {"minutes": 25, "intent": 12},
                    {"minutes": 25, "intent": "x" * 141}]
        for payload in payloads:
            with self.subTest(payload=payload):
                with self.assertRaises(HTTPError) as ctx:
                    urlopen(self.json_request("/api/start", payload))
                self.assertEqual(ctx.exception.code, 400)
        self.launcher.assert_not_called()

    @patch("web_dashboard.is_admin", return_value=False)
    def test_restore_requires_token_and_uses_uac(self, _admin):
        with urlopen(self.json_request("/api/restore", {})) as response:
            self.assertEqual(response.status, 202)
        self.launcher.assert_called_once_with("--system-restore")

    @patch("web_dashboard.is_admin", return_value=False)
    def test_rejects_invalid_study_duration_before_uac(self, _admin):
        for minutes in (10, 120, "25", True):
            with self.subTest(minutes=minutes):
                with self.assertRaises(HTTPError) as ctx:
                    urlopen(self.json_request("/api/start", {"minutes": minutes,
                                                            "intent": "Open the notebook"}))
                self.assertEqual(ctx.exception.code, 400)
        self.launcher.assert_not_called()


class TrayStartupTests(unittest.TestCase):
    @patch("focus_guard.webbrowser.open_new_tab")
    @patch("focus_guard.make_tray_icon")
    @patch("focus_guard.create_dashboard_server")
    @patch("focus_guard.FocusService")
    def test_tray_starts_localhost_without_any_tk_window(self, service_type, server_factory,
                                                         tray_factory, browser_open):
        service_type.return_value.access_token = "test-secret"
        fake_server = server_factory.return_value
        fake_icon = tray_factory.return_value
        fake_icon.run.side_effect = lambda setup=None: setup(fake_icon) if setup else None
        self.assertEqual(run_app(), 0)
        fake_icon.run.assert_called_once()
        browser_open.assert_called_once_with("http://127.0.0.1:8766/#token=test-secret")
        fake_icon.stop.assert_called_once()
        fake_server.shutdown.assert_called_once()


if __name__ == "__main__":
    unittest.main()
