"""Plan 011 quota harvesting: header parsing, persistence, /quota endpoint."""

import datetime
import io
import json
import os
import socket
import threading
from http.client import HTTPConnection, HTTPMessage
from http.server import ThreadingHTTPServer
from typing import ClassVar
from unittest import mock

import pytest

from opencode_go_proxy.app import ProxyConfig, ResponsesProxyHandler
from opencode_go_proxy.quota import (
    quota_snapshot_from_headers,
    quota_state_path,
    read_quota_state,
    record_quota_from_headers,
)
from opencode_go_proxy.upstream import call_upstream_chat, call_upstream_chat_verbatim


def make_config() -> ProxyConfig:
    return ProxyConfig(
        bind="127.0.0.1", port=8787, chat_base_url="https://up.test/v1",
        api_key_env="OPENCODE_GO_API_KEY", timeout_sec=10, max_body_bytes=1024 * 1024,
    )


@pytest.fixture(autouse=True)
def no_quota_write_interval(monkeypatch) -> None:
    """Persistence tests assert each distinct snapshot lands immediately.

    The write throttle is production behavior; the tests that exercise it
    re-enable the interval explicitly.
    """
    monkeypatch.setattr("opencode_go_proxy.quota.WRITE_INTERVAL_SECONDS", 0.0)


def chat_payload() -> dict:
    return {"model": "deepseek-v4-flash", "messages": [{"role": "user", "content": "hi"}]}


def _reset_epoch(snapshot: dict) -> float:
    return datetime.datetime.fromisoformat(snapshot["resetAt"]).timestamp()


class TestHeaderParsing:
    def test_openai_bare_scheme(self) -> None:
        snapshots = quota_snapshot_from_headers({
            "x-ratelimit-limit": "100",
            "x-ratelimit-remaining": "42",
            "x-ratelimit-reset": "2026-08-11T06:30:00Z",
        })
        assert list(snapshots) == ["openai"]
        snap = snapshots["openai"]
        assert snap["provider"] == "openai"
        assert snap["limit"] == 100
        assert snap["remaining"] == 42
        assert snap["resetAt"] == "2026-08-11T06:30:00.000Z"
        assert "sampledAt" in snap

    def test_openai_requests_family_preferred_over_tokens(self) -> None:
        snapshots = quota_snapshot_from_headers({
            "x-ratelimit-limit-requests": "500",
            "x-ratelimit-remaining-requests": "123",
            "x-ratelimit-reset-requests": "1s",
            "x-ratelimit-limit-tokens": "200000",
            "x-ratelimit-remaining-tokens": "150000",
        })
        snap = snapshots["openai"]
        assert snap["limit"] == 500
        assert snap["remaining"] == 123
        assert abs(_reset_epoch(snap) - (datetime.datetime.now(datetime.UTC).timestamp() + 1)) < 5

    def test_openai_tokens_family_fallback(self) -> None:
        snapshots = quota_snapshot_from_headers({
            "x-ratelimit-limit-tokens": "200000",
            "x-ratelimit-remaining-tokens": "150000",
        })
        snap = snapshots["openai"]
        assert snap["limit"] == 200000
        assert snap["remaining"] == 150000

    def test_anthropic_requests_scheme(self) -> None:
        snapshots = quota_snapshot_from_headers({
            "anthropic-ratelimit-requests-limit": "1000",
            "anthropic-ratelimit-requests-remaining": "999",
            "anthropic-ratelimit-requests-reset": "2026-08-11T07:00:00Z",
        })
        assert list(snapshots) == ["anthropic"]
        snap = snapshots["anthropic"]
        assert snap["limit"] == 1000
        assert snap["remaining"] == 999
        assert snap["resetAt"] == "2026-08-11T07:00:00.000Z"

    def test_anthropic_input_tokens_fallback(self) -> None:
        snapshots = quota_snapshot_from_headers({
            "anthropic-ratelimit-input-tokens-limit": "8000",
            "anthropic-ratelimit-input-tokens-remaining": "6000",
            "anthropic-ratelimit-input-tokens-reset": "2026-08-11T07:00:00Z",
        })
        assert snapshots["anthropic"]["remaining"] == 6000

    def test_headers_are_case_insensitive(self) -> None:
        msg = HTTPMessage()
        msg["X-RateLimit-Limit"] = "100"
        msg["X-RateLimit-Remaining"] = "42"
        snapshots = quota_snapshot_from_headers(msg)
        assert snapshots["openai"]["remaining"] == 42

    def test_both_schemes_capture_separate_providers(self) -> None:
        snapshots = quota_snapshot_from_headers({
            "x-ratelimit-remaining": "5",
            "anthropic-ratelimit-requests-remaining": "6",
        })
        assert snapshots["openai"]["remaining"] == 5
        assert snapshots["anthropic"]["remaining"] == 6

    def test_no_headers_degrades_to_empty(self) -> None:
        assert quota_snapshot_from_headers({}) == {}
        assert quota_snapshot_from_headers(None) == {}

    def test_garbage_remaining_is_ignored(self) -> None:
        snapshots = quota_snapshot_from_headers({
            "x-ratelimit-remaining": "many",
            "x-ratelimit-limit": "100",
        })
        assert snapshots == {}


class TestResetParsing:
    def _snap(self, reset: str) -> dict:
        return quota_snapshot_from_headers({"x-ratelimit-remaining": "1", "x-ratelimit-reset": reset})["openai"]

    def test_iso_timestamp(self) -> None:
        assert self._snap("2026-08-11T06:30:00Z")["resetAt"] == "2026-08-11T06:30:00.000Z"

    def test_duration_strings(self) -> None:
        now = datetime.datetime.now(datetime.UTC).timestamp()
        assert abs(_reset_epoch(self._snap("1s")) - (now + 1)) < 5
        assert abs(_reset_epoch(self._snap("6m0s")) - (now + 360)) < 5

    def test_numeric_epoch_and_delta(self) -> None:
        expected = datetime.datetime.fromtimestamp(1789999999, tz=datetime.UTC)
        expected_text = expected.isoformat(timespec="milliseconds").replace("+00:00", "Z")
        assert self._snap("1789999999")["resetAt"] == expected_text
        now = datetime.datetime.now(datetime.UTC).timestamp()
        assert abs(_reset_epoch(self._snap("30")) - (now + 30)) < 5

    def test_unparseable_reset_omits_field(self) -> None:
        assert "resetAt" not in self._snap("soon")


class TestPersistence:
    def test_record_then_read_round_trip(self) -> None:
        assert read_quota_state() == {"providers": {}}
        record_quota_from_headers({"x-ratelimit-limit-requests": "100", "x-ratelimit-remaining-requests": "42"})
        state = read_quota_state()
        assert state["providers"]["openai"]["remaining"] == 42
        assert os.path.exists(quota_state_path())

    def test_latest_snapshot_wins_per_provider(self) -> None:
        record_quota_from_headers({"x-ratelimit-remaining-requests": "10"})
        record_quota_from_headers({"x-ratelimit-remaining-requests": "90"})
        state = read_quota_state()
        assert state["providers"]["openai"]["remaining"] == 90
        assert len(state["providers"]) == 1

    def test_mixed_providers_kept_independently(self) -> None:
        record_quota_from_headers({"x-ratelimit-remaining": "5"})
        record_quota_from_headers({"anthropic-ratelimit-requests-remaining": "6"})
        state = read_quota_state()
        assert state["providers"]["openai"]["remaining"] == 5
        assert state["providers"]["anthropic"]["remaining"] == 6

    def test_no_headers_writes_nothing(self) -> None:
        record_quota_from_headers({})
        assert not os.path.exists(quota_state_path())

    def test_atomic_write_leaves_no_tmp(self) -> None:
        record_quota_from_headers({"x-ratelimit-remaining": "1"})
        assert not os.path.exists(quota_state_path() + ".tmp")

    def test_corrupt_state_file_degrades_to_empty(self) -> None:
        with open(quota_state_path(), "w", encoding="utf-8") as handle:
            handle.write("{not json")
        assert read_quota_state() == {"providers": {}}

    def test_io_error_swallowed(self) -> None:
        with mock.patch("opencode_go_proxy.quota.open", side_effect=OSError("boom")):
            record_quota_from_headers({"x-ratelimit-remaining": "1"})  # must not raise


class TestWriteThrottle:
    def test_identical_snapshot_skips_write(self) -> None:
        # A fixed sampledAt makes the second snapshot field-wise identical to
        # the persisted one, so the write (and its fsync) must be skipped.
        with mock.patch("opencode_go_proxy.quota._iso_at", return_value="2026-08-14T00:00:00.000Z"), \
             mock.patch("opencode_go_proxy.quota.os.fsync") as fsync:
            record_quota_from_headers({"x-ratelimit-limit-requests": "500", "x-ratelimit-remaining-requests": "42"})
            record_quota_from_headers({"x-ratelimit-limit-requests": "500", "x-ratelimit-remaining-requests": "42"})
        assert fsync.call_count == 1
        assert read_quota_state()["providers"]["openai"]["remaining"] == 42

    def test_interval_throttle_limits_writes(self, monkeypatch) -> None:
        monkeypatch.setattr("opencode_go_proxy.quota.WRITE_INTERVAL_SECONDS", 1.0)
        # 100.0s for the first write's interval check and stamp, 100.5s for
        # the second record's interval check: 0.5s < 1s, so it is dropped.
        with mock.patch("opencode_go_proxy.quota.time.monotonic", side_effect=[100.0, 100.0, 100.5]):
            record_quota_from_headers({"x-ratelimit-remaining-requests": "10"})
            record_quota_from_headers({"x-ratelimit-remaining-requests": "90"})
        assert read_quota_state()["providers"]["openai"]["remaining"] == 10
        # Once the interval has elapsed the newer snapshot persists.
        with mock.patch("opencode_go_proxy.quota.time.monotonic", return_value=102.5):
            record_quota_from_headers({"x-ratelimit-remaining-requests": "90"})
        assert read_quota_state()["providers"]["openai"]["remaining"] == 90

    def test_fresh_state_dir_not_throttled_by_previous_writes(self, monkeypatch, tmp_path) -> None:
        monkeypatch.setattr("opencode_go_proxy.quota.WRITE_INTERVAL_SECONDS", 1.0)
        first_dir = tmp_path / "quota-a"
        second_dir = tmp_path / "quota-b"
        first_dir.mkdir()
        second_dir.mkdir()
        with mock.patch.dict(os.environ, {"OPENCODE_GO_PROXY_STATE_DIR": str(first_dir)}):
            record_quota_from_headers({"x-ratelimit-remaining-requests": "10"})
        # A different state dir is a different file: the first write there must
        # not be throttled even though the previous write was milliseconds ago.
        with mock.patch.dict(os.environ, {"OPENCODE_GO_PROXY_STATE_DIR": str(second_dir)}):
            record_quota_from_headers({"x-ratelimit-remaining-requests": "20"})
        with mock.patch.dict(os.environ, {"OPENCODE_GO_PROXY_STATE_DIR": str(second_dir)}):
            assert read_quota_state()["providers"]["openai"]["remaining"] == 20

    def test_read_quota_state_memoized_until_file_changes(self) -> None:
        record_quota_from_headers({"x-ratelimit-remaining-requests": "7"})
        first = read_quota_state()
        # Unchanged file: the memo serves the second read with no parse.
        with mock.patch("opencode_go_proxy.quota.open", side_effect=AssertionError("re-read")):
            second = read_quota_state()
        assert first == second == {"providers": {"openai": {
            "provider": "openai", "remaining": 7, "sampledAt": mock.ANY,
        }}}


def ok_response(headers: dict | None = None) -> mock.Mock:
    raw = json.dumps({"choices": [{"message": {"content": "hi"}}]}).encode("utf-8")
    return mock.Mock(status=200, headers=headers or {}, read=lambda: raw,
                     __enter__=lambda s: s, __exit__=lambda *a: False)


class TestUpstreamHooks:
    def test_call_upstream_chat_records_quota(self) -> None:
        headers = {"x-ratelimit-limit-requests": "500", "x-ratelimit-remaining-requests": "432"}
        with mock.patch.dict(os.environ, {"OPENCODE_GO_API_KEY": "test-key"}), \
             mock.patch("urllib.request.urlopen", return_value=ok_response(headers)):
            _value, retries = call_upstream_chat(chat_payload(), make_config(), "req")
        assert retries == 0
        assert read_quota_state()["providers"]["openai"]["remaining"] == 432

    def test_call_upstream_chat_verbatim_records_quota(self) -> None:
        with mock.patch.dict(os.environ, {"OPENCODE_GO_API_KEY": "test-key"}), \
             mock.patch("urllib.request.urlopen", return_value=ok_response({"x-ratelimit-remaining": "7"})):
            status, _body, _retries, _ct, _retry_after = call_upstream_chat_verbatim(chat_payload(), make_config(), "req")
        assert status == 200
        assert read_quota_state()["providers"]["openai"]["remaining"] == 7

    def test_streaming_records_quota(self) -> None:
        from opencode_go_proxy.streaming import handle_streaming_request

        class FakeStream:
            status: ClassVar[int] = 200
            headers: ClassVar[dict[str, str]] = {"x-ratelimit-limit-requests": "500", "x-ratelimit-remaining-requests": "88"}
            lines: ClassVar[list[bytes]] = [
                b'data: {"id":"1","choices":[{"index":0,"delta":{"content":"hi"}}]}\n',
                b'data: {"id":"1","choices":[{"index":0,"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":1,"completion_tokens":1,"total_tokens":2}}\n',
                b"data: [DONE]\n",
            ]

            def __iter__(self):
                return iter(self.lines)

            def __enter__(self):
                return self

            def __exit__(self, *args: object) -> bool:
                return False

        wfile = io.BytesIO()
        with mock.patch.dict(os.environ, {"OPENCODE_GO_API_KEY": "test-key"}), \
             mock.patch("urllib.request.urlopen", return_value=FakeStream()):
            handle_streaming_request(
                {"model": "deepseek-v4-flash", "input": "hi", "stream": True}, make_config(), "req-stream", wfile
            )
        assert read_quota_state()["providers"]["openai"]["remaining"] == 88


@pytest.fixture
def server() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    httpd = ThreadingHTTPServer(("127.0.0.1", port), ResponsesProxyHandler)
    httpd.config = make_config()  # type: ignore[attr-defined]

    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()

    yield port

    httpd.shutdown()
    httpd.server_close()


def get(port: int, path: str) -> tuple[int, str]:
    conn = HTTPConnection("127.0.0.1", port, timeout=10)
    conn.request("GET", path)
    resp = conn.getresponse()
    raw = resp.read().decode("utf-8")
    conn.close()
    return resp.status, raw


class TestQuotaEndpoint:
    def test_quota_returns_empty_state(self, server) -> None:
        status, raw = get(server, "/quota")
        assert status == 200
        assert json.loads(raw) == {"providers": {}}

    def test_quota_alias_path(self, server) -> None:
        status, raw = get(server, "/v1/quota")
        assert status == 200
        assert json.loads(raw) == {"providers": {}}

    def test_quota_reflects_recorded_snapshot(self, server) -> None:
        record_quota_from_headers({"x-ratelimit-limit-requests": "500", "x-ratelimit-remaining-requests": "99"})
        status, raw = get(server, "/quota")
        assert status == 200
        state = json.loads(raw)
        assert state["providers"]["openai"]["remaining"] == 99


class TestMalformedHeaders:
    def test_huge_reset_does_not_raise(self) -> None:
        headers = {"x-ratelimit-remaining-requests": "5", "x-ratelimit-reset-requests": "9" * 400}
        snapshots = quota_snapshot_from_headers(headers)
        assert snapshots
        assert "resetAt" not in snapshots["openai"]

    def test_huge_remaining_duration_does_not_raise(self) -> None:
        headers = {"x-ratelimit-remaining-requests": "5", "x-ratelimit-reset-requests": ("9" * 400) + "s"}
        assert quota_snapshot_from_headers(headers)["openai"]["remaining"] == 5
