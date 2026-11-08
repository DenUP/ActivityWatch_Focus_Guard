import unittest
from datetime import datetime, timezone

from focus_guard import FocusTracker, category_for_url, classify, event_is_live


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


if __name__ == "__main__":
    unittest.main()
