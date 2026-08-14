"""Plan 013 menu bar state contract: meter/quota aggregation + /state endpoint."""

import datetime
import json
import socket
import threading
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from typing import ClassVar

import pytest

from opencode_go_proxy.app import ProxyConfig, ResponsesProxyHandler
from opencode_go_proxy.meter import record_usage_event
from opencode_go_proxy.protocol import DEFAULT_MODEL
from opencode_go_proxy.quota import quota_state_path, record_quota_from_headers
from opencode_go_proxy.state import build_state, usage_summary

UTC = datetime.UTC
TZ = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
NOW = datetime.datetime(2026, 8, 11, 10, 0, tzinfo=TZ)


@pytest.fixture(autouse=True)
def no_network_usage_poll(monkeypatch) -> None:
    """/state must never hit the live usage endpoint, keychain, or GitHub."""
    monkeypatch.setattr("opencode_go_proxy.state.poll_go_usage", lambda _config=None: None)
    monkeypatch.setattr(
        "opencode_go_proxy.updates._fetch_latest_release",
        lambda _timeout: {"tag_name": "v0.4.0", "html_url": None},
    )


def record_at(dt_utc: datetime.datetime, **kwargs) -> None:
    """Record a usage event pinned to an absolute instant."""
    record_usage_event(at=dt_utc.timestamp(), **kwargs)


def write_quota_state(providers: dict) -> None:
    with open(quota_state_path(), "w", encoding="utf-8") as handle:
        json.dump({"providers": providers}, handle)


class TestUsageSummary:
    def test_empty_meter_is_zeros_with_stable_days(self) -> None:
        summary = usage_summary(NOW)
        assert summary["todayTurns"] == 0
        assert summary["todayTokens"] == 0
        assert summary["model"] is None
        assert [day["date"] for day in summary["last7d"]] == [
            "2026-08-05", "2026-08-06", "2026-08-07", "2026-08-08",
            "2026-08-09", "2026-08-10", "2026-08-11",
        ]
        assert all(day["tokens"] == 0 for day in summary["last7d"])

    def test_today_counts_and_day_buckets(self) -> None:
        record_at(datetime.datetime(2026, 8, 11, 3, 30, tzinfo=UTC), model="deepseek-v4-flash",
                  status=200, duration_ms=100, input_tokens=40, output_tokens=10, total_tokens=50)
        record_at(datetime.datetime(2026, 8, 11, 2, 0, tzinfo=UTC), model="deepseek-v4-flash",
                  status=200, duration_ms=100, total_tokens=100)
        record_at(datetime.datetime(2026, 8, 10, 12, 0, tzinfo=UTC), model="deepseek-v4-flash",
                  status=200, duration_ms=100, total_tokens=30)
        record_at(datetime.datetime(2026, 8, 3, 12, 0, tzinfo=UTC), model="deepseek-v4-pro",
                  status=200, duration_ms=100, total_tokens=10)

        summary = usage_summary(NOW)
        assert summary["todayTurns"] == 2
        assert summary["todayTokens"] == 150
        by_day = {day["date"]: day["tokens"] for day in summary["last7d"]}
        assert by_day["2026-08-10"] == 30
        assert by_day["2026-08-11"] == 150
        assert "2026-08-03" not in by_day
        assert sum(day["tokens"] for day in summary["last7d"]) == 180
        # Model reflects the most recent event even when it falls outside the window.
        assert summary["model"] == "deepseek-v4-pro"

    def test_token_fallback_sums_input_plus_output(self) -> None:
        record_at(datetime.datetime(2026, 8, 11, 2, 0, tzinfo=UTC), model="m",
                  status=200, duration_ms=100, input_tokens=10, output_tokens=5)
        summary = usage_summary(NOW)
        assert summary["todayTokens"] == 15

    def test_corrupt_and_invalid_lines_are_skipped(self) -> None:
        record_at(datetime.datetime(2026, 8, 11, 2, 0, tzinfo=UTC), model="m",
                  status=200, duration_ms=100, total_tokens=7)
        from opencode_go_proxy.meter import usage_events_path

        with open(usage_events_path(), "a", encoding="utf-8") as handle:
            handle.write("{not json\n")
            handle.write('{"at": "not-a-time", "model": "x", "totalTokens": 9}\n')
            handle.write('[1, 2, 3]\n')
        summary = usage_summary(NOW)
        assert summary["todayTurns"] == 1
        assert summary["todayTokens"] == 7
        assert summary["model"] == "m"

    def test_state_usage_consistent_across_fold_and_rescan(self) -> None:
        # The all-provider summary and the zen rollup both read the same fold;
        # forcing a rescan must not change the numbers /state reports.
        import os as _os

        from opencode_go_proxy.meter import usage_events_path

        record_at(datetime.datetime(2026, 8, 11, 3, 30, tzinfo=UTC), model="deepseek-v4-flash",
                  status=200, duration_ms=100, total_tokens=50, provider="zen")
        record_at(datetime.datetime(2026, 8, 11, 2, 0, tzinfo=UTC), model="deepseek-v4-flash",
                  status=200, duration_ms=100, total_tokens=25)
        folded = build_state(port=8787, upstream="u", now=NOW)
        _os.utime(usage_events_path())
        rescanned = build_state(port=8787, upstream="u", now=NOW)
        assert folded["usage"]["todayTurns"] == rescanned["usage"]["todayTurns"] == 2
        assert folded["usage"]["todayTokens"] == rescanned["usage"]["todayTokens"] == 75
        assert folded["usage"]["zen"] == rescanned["usage"]["zen"] == {
            "todayTurns": 1, "todayTokens": 50, "last7d": [0, 0, 0, 0, 0, 0, 50],
        }


class TestBuildState:
    def test_empty_state_defaults(self) -> None:
        state = build_state(port=8787, upstream="https://up.test/v1", now=NOW)
        assert state["status"] == "ok"
        assert state["port"] == 8787
        assert state["upstream"] == "https://up.test/v1"
        assert state["quota"] is None
        assert state["model"] == DEFAULT_MODEL
        assert state["usage"]["todayTurns"] == 0
        assert state["usage"]["todayTokens"] == 0
        assert len(state["usage"]["last7d"]) == 7

    def test_uses_last_event_model(self) -> None:
        record_at(datetime.datetime(2026, 8, 11, 2, 0, tzinfo=UTC), model="deepseek-v4-pro",
                  status=200, duration_ms=100, total_tokens=5)
        assert build_state(port=8787, upstream="u", now=NOW)["model"] == "deepseek-v4-pro"

    def test_quota_null_when_no_snapshot(self) -> None:
        assert build_state(port=8787, upstream="u", now=NOW)["quota"] is None

    def test_quota_picks_latest_by_sampled_at(self) -> None:
        write_quota_state({
            "openai": {
                "provider": "openai", "limit": 100, "remaining": 5,
                "resetAt": "2026-08-10T00:00:00Z", "sampledAt": "2026-08-10T00:00:00Z",
            },
            "anthropic": {
                "provider": "anthropic", "remaining": 9, "sampledAt": "2026-08-11T00:00:00Z",
            },
        })
        quota = build_state(port=8787, upstream="u", now=NOW)["quota"]
        assert quota["provider"] == "anthropic"
        assert quota["remaining"] == 9
        assert "limit" not in quota
        assert "resetAt" not in quota

    def test_quota_keeps_limit_and_reset_when_present(self) -> None:
        write_quota_state({
            "openai": {
                "provider": "openai", "limit": 500, "remaining": 432,
                "resetAt": "2026-08-11T07:00:00Z", "sampledAt": "2026-08-11T01:00:00Z",
            }
        })
        quota = build_state(port=8787, upstream="u", now=NOW)["quota"]
        assert quota["limit"] == 500
        assert quota["resetAt"] == "2026-08-11T07:00:00Z"

    def test_quota_ignores_snapshot_without_remaining(self) -> None:
        write_quota_state({"openai": {"provider": "openai", "sampledAt": "2026-08-11T00:00:00Z"}})
        assert build_state(port=8787, upstream="u", now=NOW)["quota"] is None

    def test_version_and_update_keys_present(self) -> None:
        from opencode_go_proxy import __version__

        state = build_state(port=8787, upstream="u", now=NOW)
        assert state["version"] == __version__
        assert set(state["update"]) == {"available", "checked_at", "error", "latest", "release_url"}
        assert state["update"]["latest"] == "0.4.0"
        assert state["update"]["available"] is False
        assert state["update"]["error"] is None

    def test_update_block_served_from_cache(self) -> None:
        # A fresh cached check wins over the network path, so the menu bar
        # gets the last good answer even when the fetch is unreachable.
        import os

        from opencode_go_proxy import updates
        from opencode_go_proxy.meter import state_dir

        os.makedirs(state_dir(), exist_ok=True)
        with open(updates._cache_path(), "w", encoding="utf-8") as handle:
            from opencode_go_proxy import __version__ as _ver
            _parts = [int(x) for x in _ver.split(".")]
            _newer = f"{_parts[0]}.{_parts[1]}.{_parts[2] + 1}"
            json.dump({
                "checked_at": "2026-08-14T00:00:00Z",
                "latest": _newer,
                "release_url": f"https://github.com/kartikkabadi/opencode-go-proxy/releases/tag/v{_newer}",
                "error": None,
            }, handle)
        update = build_state(port=8787, upstream="u", now=NOW)["update"]
        assert update["latest"] == _newer
        assert update["available"] is True


class TestUsageKey:
    GO: ClassVar[dict[str, object]] = {
        "rolling": {"status": "ok", "percent": 26, "resetsAt": "2026-08-14T05:00:00Z"},
        "weekly": {"status": "ok", "percent": 41, "resetsAt": "2026-08-16T00:00:00Z"},
        "monthly": {"status": "ok", "percent": 12, "resetsAt": "2026-08-31T00:00:00Z"},
    }

    def test_go_usage_from_poller(self, monkeypatch) -> None:
        monkeypatch.setattr("opencode_go_proxy.state.poll_go_usage", lambda _config=None: self.GO)
        state = build_state(port=8787, upstream="u", now=NOW)
        assert state["usage"]["go"] == self.GO

    def test_go_usage_null_when_poller_returns_none(self) -> None:
        state = build_state(port=8787, upstream="u", now=NOW)
        assert state["usage"]["go"] is None

    def test_go_limits_exact_values(self) -> None:
        state = build_state(port=8787, upstream="u", now=NOW)
        assert state["usage"]["goLimits"] == {
            "monthlyDollars": 60,
            "weeklyDollars": 30,
            "rolling5hDollars": 12,
            "subscriptionMonthlyDollars": 10,
        }

    def test_zen_rollup_buckets_provider_events(self) -> None:
        record_at(datetime.datetime(2026, 8, 11, 3, 30, tzinfo=UTC), model="deepseek-v4-flash",
                  status=200, duration_ms=100, total_tokens=50, provider="zen")
        record_at(datetime.datetime(2026, 8, 11, 2, 0, tzinfo=UTC), model="deepseek-v4-flash",
                  status=200, duration_ms=100, total_tokens=100, provider="zen")
        record_at(datetime.datetime(2026, 8, 10, 12, 0, tzinfo=UTC), model="deepseek-v4-flash",
                  status=200, duration_ms=100, total_tokens=30, provider="zen")
        record_at(datetime.datetime(2026, 8, 11, 1, 0, tzinfo=UTC), model="deepseek-v4-flash",
                  status=200, duration_ms=100, total_tokens=999)
        state = build_state(port=8787, upstream="u", now=NOW)
        zen = state["usage"]["zen"]
        assert zen["todayTurns"] == 2
        assert zen["todayTokens"] == 150
        assert zen["last7d"] == [0, 0, 0, 0, 0, 30, 150]

    def test_legacy_usage_keys_still_present(self) -> None:
        state = build_state(port=8787, upstream="u", now=NOW)
        usage = state["usage"]
        assert set(usage) == {"todayTurns", "todayTokens", "last7d", "go", "goLimits", "zen"}
        assert usage["todayTurns"] == 0
        assert usage["todayTokens"] == 0
        assert len(usage["last7d"]) == 7


def make_config(port: int = 8787) -> ProxyConfig:
    return ProxyConfig(
        bind="127.0.0.1", port=port, chat_base_url="https://up.test/v1",
        api_key_env="OPENCODE_GO_API_KEY", timeout_sec=10, max_body_bytes=1024 * 1024,
    )


@pytest.fixture
def server() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    httpd = ThreadingHTTPServer(("127.0.0.1", port), ResponsesProxyHandler)
    httpd.config = make_config(port=port)  # type: ignore[attr-defined]

    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()

    yield port

    httpd.shutdown()
    httpd.server_close()


def get(port: int, path: str) -> tuple[int, dict]:
    conn = HTTPConnection("127.0.0.1", port, timeout=10)
    conn.request("GET", path)
    resp = conn.getresponse()
    raw = resp.read().decode("utf-8")
    conn.close()
    return resp.status, json.loads(raw)


class TestStateEndpoint:
    def test_state_returns_contract_shape(self, server) -> None:
        status, state = get(server, "/state")
        assert status == 200
        assert set(state) == {"status", "port", "upstream", "quota", "usage", "model", "version", "update"}
        assert state["status"] == "ok"
        assert state["port"] == server
        assert state["upstream"] == "https://up.test/v1"
        assert state["quota"] is None
        assert state["model"] == DEFAULT_MODEL
        assert set(state["usage"]) == {"todayTurns", "todayTokens", "last7d", "go", "goLimits", "zen"}
        assert state["usage"]["go"] is None
        assert state["usage"]["zen"]["last7d"] == [0, 0, 0, 0, 0, 0, 0]
        assert set(state["update"]) == {"available", "checked_at", "error", "latest", "release_url"}

    def test_state_alias_path(self, server) -> None:
        status, _state = get(server, "/v1/state")
        assert status == 200

    def test_state_reflects_meter_and_quota(self, server) -> None:
        # /state aggregates with the real clock, so the event must be "now":
        # a hardcoded past date lands outside today's bucket.
        record_at(datetime.datetime.now(UTC), model="deepseek-v4-pro",
                  status=200, duration_ms=100, total_tokens=42)
        record_quota_from_headers({"x-ratelimit-limit-requests": "500", "x-ratelimit-remaining-requests": "99"})
        status, state = get(server, "/state")
        assert status == 200
        assert state["usage"]["todayTurns"] == 1
        assert state["usage"]["todayTokens"] == 42
        assert state["model"] == "deepseek-v4-pro"
        assert state["quota"]["provider"] == "openai"
        assert state["quota"]["remaining"] == 99
        assert state["quota"]["limit"] == 500


class TestZeroTokenEstimateAndQuota:
    def test_estimated_input_tokens_counted(self, tmp_path) -> None:
        import datetime
        import os
        from unittest import mock

        from opencode_go_proxy.state import usage_summary

        state = tmp_path / "state"
        state.mkdir(exist_ok=True)
        meter_file = state / "usage-events.jsonl"
        meter_file.write_text(
            '{"at":"2026-08-11T10:00:00Z","model":"m","status":200,"inputTokens":0,'
            '"outputTokens":5,"totalTokens":0,"estimatedInputTokens":1000}\n'
        )
        with mock.patch.dict(os.environ, {"OPENCODE_GO_PROXY_STATE_DIR": str(state)}, clear=True):
            summary = usage_summary(datetime.datetime(2026, 8, 11, 12, 0, tzinfo=datetime.UTC))
        assert summary["todayTurns"] == 1
        assert summary["todayTokens"] == 1005

    def test_malformed_sampled_at_is_skipped(self, tmp_path) -> None:
        import os
        from unittest import mock

        from opencode_go_proxy.state import build_state

        state = tmp_path / "state"
        state.mkdir(exist_ok=True)
        quota_file = state / "quota-state.json"
        quota_file.write_text(
            '{"providers":{"openai":{"provider":"openai","remaining":5,"sampledAt":"not-a-time"},'
            '"anthropic":{"provider":"anthropic","remaining":9,"sampledAt":"2026-08-11T10:00:00Z"}}}'
        )
        with mock.patch.dict(os.environ, {"OPENCODE_GO_PROXY_STATE_DIR": str(state)}, clear=True):
            quota = build_state(8787, "https://upstream")["quota"]
        assert quota is not None
        assert quota["provider"] == "anthropic"
        assert quota["remaining"] == 9
