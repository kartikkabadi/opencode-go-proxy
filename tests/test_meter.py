"""Usage meter: honest append-only accounting for proxied turns."""

import datetime
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from opencode_go_proxy.meter import (
    STATE_DIR_ENV,
    record_usage_event,
    state_dir,
    usage_events_path,
    usage_summary,
)


class MeterRecordTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="ogg-meter-")
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))

    def _events(self) -> list[dict]:
        path = usage_events_path()
        if not os.path.exists(path):
            return []
        with open(path, encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]

    def _record(self, **kwargs) -> list[dict]:
        """Record under the tmp state dir and return the events written."""
        with mock.patch.dict(os.environ, {STATE_DIR_ENV: self.tmp}):
            record_usage_event(**kwargs)
            return self._events()

    def test_happy_path_records_one_line(self) -> None:
        events = self._record(
            model="deepseek-v4-flash", status=200, duration_ms=1500,
            input_tokens=10, output_tokens=5, total_tokens=15,
        )
        self.assertEqual(len(events), 1)
        e = events[0]
        self.assertEqual(e["model"], "deepseek-v4-flash")
        self.assertEqual(e["status"], 200)
        self.assertEqual(e["durationMs"], 1500)
        self.assertEqual(e["inputTokens"], 10)
        self.assertEqual(e["totalTokens"], 15)
        self.assertNotIn("streamAborted", e)
        self.assertNotIn("retries", e)

    def test_canonical_schema_fields(self) -> None:
        events = self._record(
            model="deepseek-v4-flash", status=200, duration_ms=1500,
            input_tokens=10, output_tokens=5, total_tokens=15, retries=1,
        )
        e = events[0]
        self.assertEqual(e["meteringVersion"], "opencode-go-proxy/1")
        self.assertEqual(e["provider"], "opencode-go")
        self.assertEqual(e["durationMs"], 1500)
        self.assertEqual(e["inputTokens"], 10)
        self.assertEqual(e["outputTokens"], 5)
        self.assertEqual(e["totalTokens"], 15)
        # at is ISO 8601 UTC, parseable by the reference consumers.
        parsed = datetime.datetime.fromisoformat(e["at"])
        self.assertIsNotNone(parsed)
        self.assertNotIn("input_tokens", e)
        self.assertNotIn("duration_ms", e)

    def test_stream_aborted_marks_and_omits_tokens(self) -> None:
        events = self._record(model="deepseek-v4-flash", status=502, duration_ms=800, stream_aborted=True)
        self.assertEqual(len(events), 1)
        e = events[0]
        self.assertEqual(e["status"], 502)
        self.assertEqual(e["streamAborted"], True)
        self.assertNotIn("inputTokens", e)
        self.assertNotIn("outputTokens", e)

    def test_empty_completion_marker(self) -> None:
        events = self._record(model="m", status=200, duration_ms=1, empty_completion=True)
        self.assertEqual(events[0]["emptyCompletion"], True)

    def test_estimated_input_tokens_field_kept_separate(self) -> None:
        events = self._record(
            model="m", status=200, duration_ms=1,
            input_tokens=0, estimated_input_tokens=5000,
        )
        e = events[0]
        self.assertEqual(e["inputTokens"], 0)
        self.assertEqual(e["estimatedInputTokens"], 5000)

    def test_retries_recorded_only_when_present(self) -> None:
        events = self._record(model="m", status=200, duration_ms=1, retries=2)
        self.assertEqual(events[0]["retries"], 2)
        events = self._record(model="m", status=200, duration_ms=1)
        self.assertNotIn("retries", events[1])

    def test_unclean_tokens_omitted(self) -> None:
        events = self._record(
            model="m", status=200, duration_ms=1,
            input_tokens=-3, output_tokens="x", total_tokens=True,
        )
        e = events[0]
        self.assertNotIn("inputTokens", e)
        self.assertNotIn("outputTokens", e)
        self.assertNotIn("totalTokens", e)

    def test_creates_dir_on_demand(self) -> None:
        nested = os.path.join(self.tmp, "a", "b")
        with mock.patch.dict(os.environ, {STATE_DIR_ENV: nested}):
            record_usage_event(model="m", status=200, duration_ms=1)
            self.assertTrue(os.path.exists(usage_events_path()))

    def test_io_error_swallowed(self) -> None:
        with mock.patch.dict(os.environ, {STATE_DIR_ENV: self.tmp}), \
             mock.patch("opencode_go_proxy.meter.open", side_effect=OSError("boom")):
            record_usage_event(model="m", status=200, duration_ms=1)  # must not raise

    def test_default_state_dir_is_codex_dir(self) -> None:
        # True default (env cleared, not the autouse tmp override).
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertIn(".codex", state_dir())


class MeterSummaryFoldTests(unittest.TestCase):
    """O(1) fold: aggregate equals a full scan across growth, shrink, rollover."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="ogg-meter-fold-")
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self._env = mock.patch.dict(os.environ, {STATE_DIR_ENV: self.tmp})
        self._env.start()
        self.addCleanup(self._env.stop)

    def _now(self) -> datetime.datetime:
        """A machine-local now: the fold buckets days in the local tz."""
        return datetime.datetime.now().astimezone()

    def _record(self, **kwargs) -> None:
        record_usage_event(**kwargs)

    def _force_rescan(self) -> None:
        """Touch the meter file so the fingerprint no longer matches the fold."""
        os.utime(usage_events_path())

    def _append_line(self, text: str) -> None:
        with open(usage_events_path(), "a", encoding="utf-8") as handle:
            handle.write(text + "\n")

    def _iso_now(self) -> str:
        instant = datetime.datetime.now(datetime.UTC)
        return instant.isoformat(timespec="milliseconds").replace("+00:00", "Z")

    def test_second_call_serves_aggregate_without_rereading(self) -> None:
        self._record(model="m", status=200, duration_ms=1, total_tokens=10)
        self._record(model="m", status=200, duration_ms=1, total_tokens=20)
        now = self._now()
        first = usage_summary(now)
        # The fold is warm: a second summary must not open the file at all.
        with mock.patch("opencode_go_proxy.meter.open", side_effect=AssertionError("file re-read")):
            second = usage_summary(now)
        self.assertEqual(first, second)
        self.assertEqual(first["todayTurns"], 2)
        self.assertEqual(first["todayTokens"], 30)

    def test_appended_events_fold_from_tail(self) -> None:
        now = self._now()
        self._record(model="m1", status=200, duration_ms=1, total_tokens=10)
        usage_summary(now)  # establish the fold
        # A write that bypasses record_usage_event grows the file externally.
        self._append_line(
            f'{{"at": "{self._iso_now()}", "model": "m2", "status": 200, '
            f'"provider": "zen", "totalTokens": 42}}'
        )
        agg = usage_summary(now)
        self.assertEqual(agg["todayTurns"], 2)
        self.assertEqual(agg["todayTokens"], 52)
        self._force_rescan()
        self.assertEqual(agg, usage_summary(now))

    def test_truncated_file_forces_full_rescan(self) -> None:
        now = self._now()
        self._record(model="m1", status=200, duration_ms=1, total_tokens=10)
        self._record(model="m2", status=200, duration_ms=1, total_tokens=20)
        usage_summary(now)  # establish the fold covering both events
        path = usage_events_path()
        with open(path, "rb") as handle:
            first_line = handle.readline()
        with open(path, "wb") as handle:
            handle.write(first_line)
        summary = usage_summary(now)
        self.assertEqual(summary["todayTurns"], 1)
        self.assertEqual(summary["todayTokens"], 10)
        self.assertEqual(summary["model"], "m1")

    def test_replaced_file_forces_full_rescan(self) -> None:
        now = self._now()
        self._record(model="m1", status=200, duration_ms=1, total_tokens=10)
        usage_summary(now)  # establish the fold
        replacement = usage_events_path() + ".replacement"
        with open(replacement, "w", encoding="utf-8") as handle:
            handle.write(
                f'{{"at": "{self._iso_now()}", "model": "replacement", '
                f'"status": 200, "totalTokens": 99}}'
            )
        os.replace(replacement, usage_events_path())  # new inode
        summary = usage_summary(now)
        self.assertEqual(summary["todayTurns"], 1)
        self.assertEqual(summary["todayTokens"], 99)
        self.assertEqual(summary["model"], "replacement")

    def test_provider_filtered_rollups_match_full_scan(self) -> None:
        now = self._now()
        self._record(model="zen-m", status=200, duration_ms=1, total_tokens=30, provider="zen")
        self._record(model="go-m", status=200, duration_ms=1, total_tokens=50)
        self._record(model="native-m", status=200, duration_ms=1, total_tokens=70, provider="native")
        usage_summary(now)  # establish the fold
        for provider in (None, "zen", "opencode-go", "native"):
            rolled = usage_summary(now, provider=provider)
            self._force_rescan()
            self.assertEqual(rolled, usage_summary(now, provider=provider))
        self.assertEqual(usage_summary(now, provider="zen")["todayTokens"], 30)
        self.assertEqual(usage_summary(now, provider="opencode-go")["todayTokens"], 50)
        self.assertEqual(usage_summary(now)["todayTokens"], 150)

    def test_midnight_rollover_moves_today(self) -> None:
        now = self._now()
        self._record(model="m", status=200, duration_ms=1, total_tokens=40)
        today = usage_summary(now)
        self.assertEqual(today["todayTurns"], 1)
        self.assertEqual(today["todayTokens"], 40)
        tomorrow = now + datetime.timedelta(days=1)
        rolled = usage_summary(tomorrow)
        self.assertEqual(rolled["todayTurns"], 0)
        self.assertEqual(rolled["todayTokens"], 0)
        by_day = {day["date"]: day["tokens"] for day in rolled["last7d"]}
        self.assertEqual(by_day[now.date().isoformat()], 40)
        self._force_rescan()
        self.assertEqual(rolled, usage_summary(tomorrow))

    def test_concurrent_records_are_safe(self) -> None:
        import threading

        now = self._now()
        self._record(model="m0", status=200, duration_ms=1, total_tokens=1)
        usage_summary(now)  # establish the fold
        recorded_counts: list[int] = []

        def worker() -> None:
            recorded = 0
            for index in range(25):
                record_usage_event(
                    model=f"m{index}", status=200, duration_ms=1, total_tokens=index + 1
                )
                recorded += 1
            recorded_counts.append(recorded)

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        # A worker that raised would never append, so the count check below
        # would fail on its missing entry.
        self.assertEqual(recorded_counts, [25, 25, 25, 25])
        agg = usage_summary(now)
        self.assertEqual(agg["todayTurns"], 1 + 4 * 25)
        self.assertEqual(agg["todayTokens"], 1 + 4 * sum(range(1, 26)))
        self._force_rescan()
        self.assertEqual(agg, usage_summary(now))

    def test_unreadable_file_degrades_to_zeros(self) -> None:
        now = self._now()
        self._record(model="m", status=200, duration_ms=1, total_tokens=10)
        usage_summary(now)  # establish the fold
        with mock.patch("opencode_go_proxy.meter.os.stat", side_effect=OSError("boom")):
            summary = usage_summary(now)
        self.assertEqual(summary["todayTurns"], 0)
        self.assertEqual(summary["todayTokens"], 0)
        self.assertIsNone(summary["model"])
        self.assertEqual(len(summary["last7d"]), 7)


if __name__ == "__main__":
    unittest.main()
