"""Usage meter: honest append-only accounting for proxied turns."""

import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from opencode_go_proxy.meter import STATE_DIR_ENV, record_usage_event, state_dir, usage_events_path


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
        self.assertEqual(e["duration_ms"], 1500)
        self.assertEqual(e["input_tokens"], 10)
        self.assertEqual(e["total_tokens"], 15)
        self.assertNotIn("streamAborted", e)
        self.assertNotIn("retries", e)

    def test_stream_aborted_marks_and_omits_tokens(self) -> None:
        events = self._record(model="deepseek-v4-flash", status=502, duration_ms=800, stream_aborted=True)
        self.assertEqual(len(events), 1)
        e = events[0]
        self.assertEqual(e["status"], 502)
        self.assertEqual(e["streamAborted"], True)
        self.assertNotIn("input_tokens", e)
        self.assertNotIn("output_tokens", e)

    def test_empty_completion_marker(self) -> None:
        events = self._record(model="m", status=200, duration_ms=1, empty_completion=True)
        self.assertEqual(events[0]["emptyCompletion"], True)

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
        self.assertNotIn("input_tokens", e)
        self.assertNotIn("output_tokens", e)
        self.assertNotIn("total_tokens", e)

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
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertIn(".codex", state_dir())


if __name__ == "__main__":
    unittest.main()
