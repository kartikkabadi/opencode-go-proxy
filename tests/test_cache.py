"""Unit tests for the prefix-cache accounting tracker."""

import unittest

from opencode_go_proxy.cache import CacheTracker


class CacheTrackerTests(unittest.TestCase):
    def test_empty_tracker_reports_no_ratio(self) -> None:
        snap = CacheTracker().snapshot()
        self.assertEqual(snap["totals"]["hit_ratio"], None)
        self.assertEqual(snap["totals"]["requests"], 0)
        self.assertEqual(snap["models"], [])

    def test_records_hits_and_misses_per_model(self) -> None:
        tracker = CacheTracker()
        tracker.record("deepseek-v4-flash", hit=900, miss=10)
        tracker.record("deepseek-v4-flash", hit=950, miss=5)
        tracker.record("mimo-v2.5", hit=0, miss=100)

        snap = tracker.snapshot()
        self.assertEqual(len(snap["models"]), 2)

        ds = next(m for m in snap["models"] if m["model"] == "deepseek-v4-flash")
        self.assertEqual(ds["cache_hit_tokens"], 1850)
        self.assertEqual(ds["cache_miss_tokens"], 15)
        self.assertEqual(ds["requests"], 2)
        self.assertAlmostEqual(ds["hit_ratio"], 1850 / 1865, places=6)

        mm = next(m for m in snap["models"] if m["model"] == "mimo-v2.5")
        self.assertEqual(mm["hit_ratio"], 0.0)

        self.assertEqual(snap["totals"]["cache_hit_tokens"], 1850)
        self.assertEqual(snap["totals"]["cache_miss_tokens"], 115)
        self.assertAlmostEqual(snap["totals"]["hit_ratio"], 1850 / 1965, places=6)

    def test_negative_counts_are_ignored(self) -> None:
        tracker = CacheTracker()
        tracker.record("deepseek-v4-flash", hit=-1, miss=5)
        snap = tracker.snapshot()
        self.assertEqual(snap["totals"]["cache_miss_tokens"], 0)

    def test_unknown_model_records_under_unknown_key(self) -> None:
        tracker = CacheTracker()
        tracker.record(None, hit=10, miss=0)
        snap = tracker.snapshot()
        self.assertEqual(snap["models"][0]["model"], "unknown")


if __name__ == "__main__":
    unittest.main()
