"""GitHub release check: semver compare, TTL cache, offline degradation."""

import io
import json
import os
import urllib.error
from unittest import mock

from opencode_go_proxy import __version__, updates

_v = [int(x) for x in __version__.split(".")]
_NEWER = f"{_v[0]}.{_v[1]}.{_v[2] + 1}"
_NEWER_TAG = f"v{_NEWER}"
RELEASE_URL = f"https://github.com/kartikkabadi/opencode-go-proxy/releases/tag/{_NEWER_TAG}"


class MockResp:
    def __init__(self, body: bytes, status: int = 200):
        self._body = body
        self.status = status
        self.headers = {}

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _release(tag: str, html_url: str | None = RELEASE_URL) -> MockResp:
    return MockResp(json.dumps({"tag_name": tag, "html_url": html_url}).encode())


def _urlopen_path() -> str:
    return "opencode_go_proxy.updates.urllib.request.urlopen"


def _cache_payload() -> dict:
    with open(updates._cache_path(), encoding="utf-8") as handle:
        return json.load(handle)


class TestCheckForUpdates:
    def test_newer_release_reported_available(self) -> None:
        with mock.patch(_urlopen_path(), return_value=_release(_NEWER_TAG)):
            info = updates.check_for_updates(force=True)
        assert info.latest == _NEWER
        assert info.available is True
        assert info.release_url == RELEASE_URL
        assert info.error is None
        assert info.checked_at is not None

    def test_current_version_is_not_an_update(self) -> None:
        with mock.patch(_urlopen_path(), return_value=_release(f"v{__version__}")):
            info = updates.check_for_updates(force=True)
        assert info.latest == __version__
        assert info.available is False

    def test_older_release_is_not_an_update(self) -> None:
        with mock.patch(_urlopen_path(), return_value=_release("v0.3.1")):
            info = updates.check_for_updates(force=True)
        assert info.latest == "0.3.1"
        assert info.available is False

    def test_prerelease_ignored_even_when_numeric_part_newer(self) -> None:
        with mock.patch(_urlopen_path(), return_value=_release("v0.4.5-rc.1")):
            info = updates.check_for_updates(force=True)
        assert info.latest == "0.4.5-rc.1"
        assert info.available is False

    def test_v_prefix_stripped_from_latest(self) -> None:
        with mock.patch(_urlopen_path(), return_value=_release(_NEWER_TAG)):
            info = updates.check_for_updates(force=True)
        assert info.latest == _NEWER
        assert not info.latest.startswith("v")

    def test_git_api_base_comes_from_env(self) -> None:
        with mock.patch.dict(os.environ, {"OPENCODE_GO_PROXY_GITHUB_API": "https://api.example.test"}, clear=False), \
             mock.patch(_urlopen_path(), return_value=_release(_NEWER_TAG)) as urlopen:
            updates.check_for_updates(force=True)
        request = urlopen.call_args.args[0]
        assert request.full_url == "https://api.example.test/repos/kartikkabadi/opencode-go-proxy/releases/latest"
        assert request.headers.get("User-agent") == f"opencode-go-proxy/{__version__}"


class TestTtlCache:
    def test_cache_hit_skips_network(self) -> None:
        with mock.patch(_urlopen_path(), return_value=_release(_NEWER_TAG)):
            first = updates.check_for_updates(force=True)
        assert first.latest == _NEWER
        with mock.patch(_urlopen_path(), side_effect=AssertionError("network must not be touched")):
            second = updates.check_for_updates()
        assert second.latest == _NEWER
        assert second.available is True

    def test_force_bypasses_cache(self) -> None:
        with mock.patch(_urlopen_path(), return_value=_release(_NEWER_TAG)):
            updates.check_for_updates(force=True)
        with mock.patch(_urlopen_path(), return_value=_release("v0.4.1")):
            info = updates.check_for_updates(force=True)
        assert info.latest == "0.4.1"

    def test_expired_ttl_rechecks(self) -> None:
        with mock.patch.dict(os.environ, {"OPENCODE_GO_PROXY_UPDATE_TTL_HOURS": "0"}, clear=False), \
             mock.patch(_urlopen_path(), side_effect=[_release(_NEWER_TAG), _release("v0.4.1")]):
            first = updates.check_for_updates(force=True)
            second = updates.check_for_updates()
        assert first.latest == _NEWER
        assert second.latest == "0.4.1"

    def test_cache_file_mode_0600_under_state_dir(self) -> None:
        import stat

        with mock.patch(_urlopen_path(), return_value=_release(_NEWER_TAG)):
            updates.check_for_updates(force=True)
        assert updates._cache_path().endswith("update-check.json")
        mode = os.stat(updates._cache_path()).st_mode & 0o777
        assert mode == stat.S_IRUSR | stat.S_IWUSR


class TestErrorPaths:
    def test_offline_error_sets_error_field(self) -> None:
        with mock.patch(_urlopen_path(), side_effect=urllib.error.URLError("offline")):
            info = updates.check_for_updates(force=True)
        assert info.error is not None
        assert "URLError" in info.error
        assert info.latest is None
        assert info.available is False

    def test_offline_error_preserves_cached_latest(self) -> None:
        with mock.patch(_urlopen_path(), return_value=_release(_NEWER_TAG)):
            updates.check_for_updates(force=True)
        with mock.patch(_urlopen_path(), side_effect=urllib.error.URLError("offline")):
            info = updates.check_for_updates(force=True)
        assert info.error is not None
        assert info.latest == _NEWER
        assert info.available is True
        assert info.release_url == RELEASE_URL

    def test_http_error_sets_error_field(self) -> None:
        import io

        error = urllib.error.HTTPError(
            "https://api.github.com/repos/kartikkabadi/opencode-go-proxy/releases/latest",
            403, "rate limited", {}, io.BytesIO(b"")
        )
        with mock.patch(_urlopen_path(), side_effect=error):
            info = updates.check_for_updates(force=True)
        assert info.error is not None
        assert "HTTPError" in info.error

    def test_error_result_is_cached_for_ttl(self) -> None:
        with mock.patch(_urlopen_path(), side_effect=urllib.error.URLError("offline")):
            updates.check_for_updates(force=True)
        cached = _cache_payload()
        assert cached["error"] is not None
        assert cached["checked_at"] is not None


class TestVersionPayload:
    def test_payload_shape_and_update_keys(self) -> None:
        with mock.patch(_urlopen_path(), return_value=_release(_NEWER_TAG)):
            payload = updates.version_payload(force=True)
        assert set(payload) == {"version", "git_commit", "update"}
        assert payload["version"] == __version__
        assert set(payload["update"]) == {"available", "checked_at", "error", "latest", "release_url"}
        assert payload["update"]["available"] is True
        assert payload["update"]["latest"] == _NEWER
        assert payload["update"]["error"] is None

    def test_git_commit_is_short_hash_or_none(self) -> None:
        commit = updates._git_commit()
        assert commit is None or (
            len(commit) == 8 and all(c in "0123456789abcdef" for c in commit)
        )




class _FakeHandler(io.BytesIO):
    """Minimal BaseHTTPRequestHandler surface for handler-level tests."""

    def makefile(self, *args, **kwargs):
        return self


class TestVersionEndpointForce:
    """GET /version?force=1 bypasses the TTL cache (handler-level)."""

    def _handler_version(self, path: str) -> dict:
        from opencode_go_proxy.app import ResponsesProxyHandler

        wfile = _FakeHandler()
        handler = ResponsesProxyHandler.__new__(ResponsesProxyHandler)
        handler.headers = {"Host": "127.0.0.1:8787"}
        handler.wfile = wfile
        handler.rfile = _FakeHandler(b"GET " + path.encode() + b" HTTP/1.1")
        handler.path = path
        handler.command = "GET"
        handler.client_address = ("127.0.0.1", 55555)
        handler.close_connection = False
        handler.server = None
        handler.requestline = "GET " + path + " HTTP/1.1"
        handler.request_version = "HTTP/1.1"
        handler.server_version = "BaseHTTP/0.6"
        handler.sys_version = ""
        handler.error_message_format = ""
        handler.error_content_type = ""
        handler.log_error = lambda *a, **k: None
        handler.log_message = lambda *a, **k: None
        handler.send_error = lambda *a, **k: None
        handler.send_response = lambda *a, **k: None
        handler.send_header = lambda *a, **k: None
        handler.end_headers = lambda *a, **k: None
        handler.do_GET()
        body = wfile.getvalue().decode()
        json_part = body.split("\r\n\r\n")[-1]
        return json.loads(json_part)

    def test_force_query_bypasses_cache(self) -> None:
        with mock.patch(_urlopen_path(), return_value=_release(_NEWER_TAG)):
            updates.check_for_updates(force=True)
        with mock.patch(_urlopen_path(), return_value=_release(_NEWER_TAG)):
            payload = self._handler_version("/version?force=1")
        assert payload["update"]["latest"] == _NEWER
        assert payload["update"]["checked_at"] is not None

    def test_no_force_reads_fresh_cache(self) -> None:
        payload = self._handler_version("/v1/version")
        assert set(payload) == {"version", "git_commit", "update"}

    def test_force_zero_behaves_like_absent(self) -> None:
        payload = self._handler_version("/version?force=0")
        assert set(payload) == {"version", "git_commit", "update"}
