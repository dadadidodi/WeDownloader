import tempfile
import unittest
from pathlib import Path

from wedownloader.progress import ProgressTracker


class ProgressTests(unittest.TestCase):
    def test_progress_tracker_persists_run_state(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "progress.json"
            tracker = ProgressTracker(path)

            tracker.start_run(
                {
                    "include_published": True,
                    "include_drafts": True,
                    "limit": 3,
                    "download_image_materials": False,
                }
            )
            tracker.set_articles_discovered(3)
            tracker.set_phase("articles")
            tracker.start_item("article", "标题", "draft:1:0")
            tracker.record_assets(
                [
                    {"status": "downloaded"},
                    {"status": "failed"},
                    {"status": "needs_manual_fetch"},
                    {"status": "skipped"},
                ]
            )
            tracker.finish_item("article", "标题", "draft:1:0")
            tracker.complete()

            loaded = ProgressTracker.load(path)

        self.assertEqual(loaded.data["status"], "completed")
        self.assertEqual(loaded.data["phase"], "completed")
        self.assertEqual(loaded.data["counters"]["articles_discovered"], 3)
        self.assertEqual(loaded.data["counters"]["items_succeeded"], 1)
        self.assertEqual(loaded.data["counters"]["assets_downloaded"], 1)
        self.assertEqual(loaded.data["counters"]["assets_failed"], 1)
        self.assertEqual(loaded.data["counters"]["assets_needs_manual_fetch"], 2)

    def test_fail_item_records_recent_error(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "progress.json"
            tracker = ProgressTracker(path)

            tracker.start_run({})
            tracker.fail_item("article", "坏文章", "draft:2:0", "boom")
            loaded = ProgressTracker.load(path)

        self.assertEqual(loaded.data["counters"]["items_failed"], 1)
        self.assertEqual(loaded.data["recent_errors"][0]["error"], "boom")


if __name__ == "__main__":
    unittest.main()
