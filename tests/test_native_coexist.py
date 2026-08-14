"""Native + Go + Zen coexistence: routing isolation, auth isolation, concurrency.

The offline block runs the real proxy in-process against a single local
recording backend that stands in for every upstream (native Responses API,
opencode-go chat completions, zen chat completions). Every leg is
deterministic and network-free, so the whole matrix runs in CI.

The live block (skipped unless NATIVE_COEXIST_LIVE=1) repeats the go/zen legs
against the real opencode.ai upstreams with the keychain key, so the
"real upstream response, not the mock" claim is verified against production.
"""

from __future__ import annotations

import json
import os
import socket
import threading
from contextlib import contextmanager
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from typing import ClassVar
from unittest import mock

import pytest

from opencode_go_proxy import passthrough
from opencode_go_proxy.app import ProxyConfig, ResponsesProxyHandler
from opencode_go_proxy.meter import state_dir
from opencode_go_proxy.secrets import clear_api_key_cache

CLIENT_AUTH = "Bearer CLIENT_FAKE_TOKEN"
GO_FAKE_KEY = "sk-test-go-key-not-secret"
NATIVE_TEXT = "MOCK_NATIVE"
GO_TEXT = "MOCK_GO"
ZEN_TEXT = "MOCK_ZEN"

NATIVE_BASE_PATH = "/codex"
GO_BASE_PATH = "/go"
ZEN_BASE_PATH = "/zen"

NATIVE_RESPONSE: dict = {
    "id": "mock-1",
    "object": "response",
    "status": "completed",
    "output": [{"type": "message", "content": [{"type": "output_text", "text": NATIVE_TEXT}]}],
}

ZEN_ERROR_BODY: dict = {
    "type": "error",
    "error": {"type": "RateLimitError", "message": "free tier limit exceeded"},
    "metadata": {},
}


def _chat_completion(text: str) -> dict:
    return {
        "id": "cmpl-mock",
        "object": "chat.completion",
        "model": "mock",
        "choices": [
            {"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": text}}
        ],
        "usage": {"prompt_tokens": 4, "completion_tokens": 2, "total_tokens": 6},
    }


class _RecordingBackend(BaseHTTPRequestHandler):
    """One local backend for every upstream role, chosen by URL prefix.

    /codex/* -> native Responses API (returns NATIVE_RESPONSE)
    /go/*    -> opencode-go chat completions (returns _chat_completion(GO_TEXT))
    /zen/*   -> zen chat completions (429 zen envelope for the free model,
                otherwise _chat_completion(ZEN_TEXT))
    """

    captured: ClassVar[list[dict]] = []

    def _record_request(self) -> dict:
        length = int(self.headers.get("content-length", "0"))
        body = self.rfile.read(length)
        record = {
            "path": self.path,
            "headers": {name.lower(): value for name, value in self.headers.items()},
            "body": body,
        }
        type(self).captured.append(record)
        return record

    def _emit(self, status: int, content_type: str, payload: bytes) -> None:
        self.send_response(status)
        self.send_header("content-type", content_type)
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self) -> None:
        record = self._record_request()
        if self.path.startswith(NATIVE_BASE_PATH):
            self._emit(200, "application/json", json.dumps(NATIVE_RESPONSE).encode())
            return
        if self.path.startswith(GO_BASE_PATH):
            self._emit(200, "application/json", json.dumps(_chat_completion(GO_TEXT)).encode())
            return
        if self.path.startswith(ZEN_BASE_PATH):
            try:
                model = json.loads(record["body"]).get("model", "")
            except (ValueError, TypeError):
                model = ""
            if model == "deepseek-v4-flash-free":
                self._emit(429, "application/json", json.dumps(ZEN_ERROR_BODY).encode())
                return
            self._emit(200, "application/json", json.dumps(_chat_completion(ZEN_TEXT)).encode())
            return
        self._emit(404, "application/json", b'{"error":"unmapped path"}')

    def log_message(self, format: str, *args: object) -> None:
        pass


def _start_server(handler: type[BaseHTTPRequestHandler]) -> tuple[int, ThreadingHTTPServer]:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    httpd = ThreadingHTTPServer(("127.0.0.1", port), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return port, httpd


@pytest.fixture()
def backend() -> str:
    _RecordingBackend.captured = []
    port, httpd = _start_server(_RecordingBackend)
    yield f"http://127.0.0.1:{port}"
    httpd.shutdown()
    httpd.server_close()


def _make_config(chat_base_url: str) -> ProxyConfig:
    return ProxyConfig(
        bind="127.0.0.1",
        port=8791,
        chat_base_url=chat_base_url,
        api_key_env="OPENCODE_GO_API_KEY",
        timeout_sec=15,
        max_body_bytes=20 * 1024 * 1024,
    )


@pytest.fixture()
def proxy_server(backend: str) -> int:
    port, httpd = _start_server(ResponsesProxyHandler)
    httpd.config = _make_config(chat_base_url=f"{backend}{GO_BASE_PATH}")  # type: ignore[attr-defined]
    yield port
    httpd.shutdown()
    httpd.server_close()


@pytest.fixture()
def offline_env(backend: str):
    """Point every upstream leg at the local recording backend, offline."""
    env = {
        passthrough.NATIVE_BASE_URL_ENV: f"{backend}{NATIVE_BASE_PATH}",
        passthrough.NATIVE_INSECURE_ENV: "1",
        "OPENCODE_ZEN_BASE_URL": f"{backend}{ZEN_BASE_PATH}",
        "OPENCODE_GO_API_KEY": GO_FAKE_KEY,
        "OPENCODE_GO_PROXY_MAX_RETRIES": "0",
    }
    with mock.patch.dict(os.environ, env):
        yield


@pytest.fixture(autouse=True)
def _fresh_api_key_cache():
    """Each test resolves its own key: offline tests must never reuse a
    keychain credential another module (or a live test) cached."""
    clear_api_key_cache()
    yield
    clear_api_key_cache()


def _seed_native_capture(*slugs: str) -> None:
    state = state_dir()
    os.makedirs(state, exist_ok=True)
    with open(os.path.join(state, "native-models.json"), "w") as handle:
        json.dump(
            {
                "captured_at": "2026-08-14T00:00:00Z",
                "models": [{"slug": slug} for slug in slugs],
            },
            handle,
        )


def _seed_zen_capture(*model_ids: str) -> None:
    state = state_dir()
    os.makedirs(state, exist_ok=True)
    with open(os.path.join(state, "zen-models.json"), "w") as handle:
        json.dump(
            {"fetched_at": "2026-08-14T00:00:00Z", "models": [{"id": model_id} for model_id in model_ids]},
            handle,
        )
    with open(os.path.join(state, "zen-catalog.json"), "w") as handle:
        json.dump(
            {
                "version": 1,
                "fetched_at": "2026-08-14T00:00:00Z",
                "models": {model_id: {"family": "openai_chat"} for model_id in model_ids},
            },
            handle,
        )


def _partition_captured() -> tuple[list[dict], list[dict], list[dict]]:
    native = [r for r in _RecordingBackend.captured if r["path"].startswith(NATIVE_BASE_PATH)]
    go = [r for r in _RecordingBackend.captured if r["path"].startswith(GO_BASE_PATH)]
    zen = [r for r in _RecordingBackend.captured if r["path"].startswith(ZEN_BASE_PATH)]
    return native, go, zen


def _auth_values(record: dict) -> list[str]:
    return [value for name, value in record["headers"].items() if name == "authorization"]


def _post(port: int, payload: dict, auth: str | None = None) -> SimpleNamespace:
    headers = {"content-type": "application/json"}
    if auth is not None:
        headers["authorization"] = auth
    conn = HTTPConnection("127.0.0.1", port, timeout=15)
    conn.request("POST", "/v1/responses", json.dumps(payload).encode(), headers)
    resp = conn.getresponse()
    raw = resp.read()
    conn.close()
    return SimpleNamespace(status=resp.status, raw=raw)


def _get(port: int, path: str) -> SimpleNamespace:
    conn = HTTPConnection("127.0.0.1", port, timeout=15)
    conn.request("GET", path)
    resp = conn.getresponse()
    raw = resp.read()
    conn.close()
    return SimpleNamespace(status=resp.status, raw=raw)


def test_native_relays_verbatim_with_client_auth_only(backend: str, proxy_server: int, offline_env) -> None:
    """(a) A bare native model relays to the native backend with the client's
    own Authorization header and nothing else — the go key never appears."""
    _seed_native_capture("gpt-5.6-terra")
    payload = {"model": "gpt-5.6-terra", "input": "hi", "stream": False}
    resp = _post(proxy_server, payload, auth=CLIENT_AUTH)

    assert resp.status == 200
    assert json.loads(resp.raw)["output"][0]["content"][0]["text"] == NATIVE_TEXT

    native, go, zen = _partition_captured()
    assert go == [] and zen == []
    assert len(native) == 1
    recorded = native[0]
    assert recorded["path"] == f"{NATIVE_BASE_PATH}/v1/responses"
    assert json.loads(recorded["body"]) == payload
    # Exactly one Authorization header, the client's; no second auth header
    # (the go key would arrive as "authorization" if the relay attached it).
    assert _auth_values(recorded) == [CLIENT_AUTH]
    assert GO_FAKE_KEY not in json.dumps(recorded["headers"])


def test_go_route_never_touches_native_backend(backend: str, proxy_server: int, offline_env) -> None:
    """(b) opencode-go/ always translates through the go upstream; the native
    backend must not see the request, and the client auth must not leak
    upstream (the proxy substitutes its own key)."""
    resp = _post(proxy_server, {"model": "opencode-go/deepseek-v4-flash", "input": "hi"}, auth=CLIENT_AUTH)

    assert resp.status == 200
    body = json.loads(resp.raw)
    assert body["object"] == "response"
    assert body["output"][0]["content"][0]["text"] == GO_TEXT

    native, go, zen = _partition_captured()
    assert native == [] and zen == []
    assert len(go) == 1
    assert go[0]["path"] == f"{GO_BASE_PATH}/chat/completions"
    assert _auth_values(go[0]) == [f"Bearer {GO_FAKE_KEY}"]
    assert CLIENT_AUTH not in json.dumps(go[0]["headers"])


def test_zen_free_model_relays_upstream_error_envelope(backend: str, proxy_server: int, offline_env) -> None:
    """(c) zen/ always goes to the zen upstream; an upstream error is relayed
    with the zen error type, never replaced by the native backend's body."""
    resp = _post(
        proxy_server, {"model": "zen/deepseek-v4-flash-free", "input": "hi"}, auth=CLIENT_AUTH
    )

    assert resp.status == 429
    assert NATIVE_TEXT not in resp.raw.decode("utf-8", "replace")
    body = json.loads(resp.raw)
    assert body["error"]["type"] == "RateLimitError"

    native, go, zen = _partition_captured()
    assert native == [] and go == []
    assert len(zen) == 1
    assert zen[0]["path"] == f"{ZEN_BASE_PATH}/chat/completions"
    assert json.loads(zen[0]["body"])["model"] == "deepseek-v4-flash-free"
    assert _auth_values(zen[0]) == [f"Bearer {GO_FAKE_KEY}"]


def test_zen_route_translates_success(backend: str, proxy_server: int, offline_env) -> None:
    """A zen model on a working upstream round-trips to a Responses object
    without ever touching the native backend."""
    _seed_zen_capture("deepseek-v4-flash")
    resp = _post(proxy_server, {"model": "zen/deepseek-v4-flash", "input": "hi"})

    assert resp.status == 200
    body = json.loads(resp.raw)
    assert body["object"] == "response"
    assert body["output"][0]["content"][0]["text"] == ZEN_TEXT

    native, go, zen = _partition_captured()
    assert native == [] and go == []
    assert len(zen) == 1
    assert json.loads(zen[0]["body"])["model"] == "deepseek-v4-flash"


def test_concurrent_requests_do_not_cross_contaminate(backend: str, proxy_server: int, offline_env) -> None:
    """(d) native + go + zen fired at the same time each get their own result
    and the native backend log shows exactly the native request."""
    _seed_native_capture("gpt-5.6-terra")
    payloads = [
        {"model": "gpt-5.6-terra", "input": "hi", "stream": False},
        {"model": "opencode-go/deepseek-v4-flash", "input": "hi"},
        {"model": "zen/deepseek-v4-flash-free", "input": "hi"},
    ]
    results: list[SimpleNamespace | None] = [None] * len(payloads)
    failures: list[Exception] = []
    barrier = threading.Barrier(len(payloads))

    def fire(index: int, payload: dict) -> None:
        barrier.wait()
        try:
            results[index] = _post(proxy_server, payload, auth=CLIENT_AUTH)
        except Exception as exc:  # noqa: BLE001 - surface any transport failure
            failures.append(exc)

    threads = [threading.Thread(target=fire, args=(index, payload)) for index, payload in enumerate(payloads)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert failures == []
    native_result = json.loads(results[0].raw)
    assert results[0].status == 200 and native_result["id"] == "mock-1"
    go_result = json.loads(results[1].raw)
    assert results[1].status == 200 and go_result["output"][0]["content"][0]["text"] == GO_TEXT
    zen_result = json.loads(results[2].raw)
    assert results[2].status == 429 and zen_result["error"]["type"] == "RateLimitError"

    native, go, zen = _partition_captured()
    assert len(native) == 1 and len(go) == 1 and len(zen) == 1
    assert json.loads(native[0]["body"])["model"] == "gpt-5.6-terra"
    assert json.loads(go[0]["body"])["model"] == "deepseek-v4-flash"
    assert json.loads(zen[0]["body"])["model"] == "deepseek-v4-flash-free"


def test_models_list_serves_each_provider_without_collision(backend: str, proxy_server: int, offline_env) -> None:
    """(e) /v1/models serves native bare, opencode-go bare, and zen prefixed;
    the same upstream id from go and zen stays two distinct ids."""
    from opencode_go_proxy import catalog as proxy_catalog

    _seed_native_capture("gpt-5.6-terra")
    _seed_zen_capture("deepseek-v4-flash")
    proxy_catalog.render_merged_catalog()

    resp = _get(proxy_server, "/v1/models")
    assert resp.status == 200
    ids = [entry["id"] for entry in json.loads(resp.raw)["data"]]

    assert "gpt-5.6-terra" in ids          # native, bare
    assert "deepseek-v4-flash" in ids      # opencode-go, bare
    assert "zen/deepseek-v4-flash" in ids  # zen, prefixed
    # The go bare slug and the zen-prefixed slug are distinct ids: the zen
    # model never shadows (or duplicates) the go model under the bare slug.
    assert ids.count("deepseek-v4-flash") == 1
    assert ids.count("zen/deepseek-v4-flash") == 1
    assert len(ids) == len(set(ids))


LIVE = os.environ.get("NATIVE_COEXIST_LIVE") == "1"
live_only = pytest.mark.skipif(not LIVE, reason="live upstream test; set NATIVE_COEXIST_LIVE=1")


@contextmanager
def _cleared_key_env():
    """Temporarily drop every key env var so resolution falls to the keychain."""
    key_envs = ("OPENCODE_GO_API_KEY", "OPENCODE_API_KEY")
    saved = {name: os.environ.get(name) for name in key_envs}
    for name in key_envs:
        os.environ.pop(name, None)
    try:
        yield
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def _live_proxy(backend: str) -> tuple[int, ThreadingHTTPServer]:
    """Real-upstream proxy: default go base (opencode.ai) in the config, only
    the native leg pointed at the recording backend."""
    port, httpd = _start_server(ResponsesProxyHandler)
    httpd.config = _make_config(chat_base_url="https://opencode.ai/zen/go/v1")  # type: ignore[attr-defined]
    return port, httpd


@live_only
def test_live_go_route_hits_real_upstream_not_mock(backend: str) -> None:
    """(b live) opencode-go/deepseek-v4-flash goes to the real opencode.ai go
    upstream with the keychain key; the native mock sees nothing and the
    response is a real upstream verdict (200 or an upstream 4xx), never
    MOCK_NATIVE."""
    port, httpd = _live_proxy(backend)
    try:
        with _cleared_key_env(), mock.patch.dict(
            os.environ,
            {
                passthrough.NATIVE_BASE_URL_ENV: f"{backend}{NATIVE_BASE_PATH}",
                passthrough.NATIVE_INSECURE_ENV: "1",
                "OPENCODE_GO_PROXY_MAX_RETRIES": "0",
            },
        ):
            resp = _post(
                port,
                {"model": "opencode-go/deepseek-v4-flash", "input": "hi"},
                auth=CLIENT_AUTH,
            )
    finally:
        httpd.shutdown()
        httpd.server_close()

    assert NATIVE_TEXT not in resp.raw.decode("utf-8", "replace")
    native, _go, zen = _partition_captured()
    assert native == [] and zen == []
    assert resp.status in (200, 401, 403, 429)
    if resp.status == 200:
        body = json.loads(resp.raw)
        assert body["object"] == "response"
        message_item = next(item for item in body["output"] if item.get("type") == "message")
        assert message_item["content"][0]["text"] not in ("", NATIVE_TEXT)


@live_only
def test_live_zen_route_hits_real_upstream_not_mock(backend: str) -> None:
    """(c live) zen/deepseek-v4-flash-free goes to the real zen upstream; a
    free-tier 429 with the zen error envelope is a valid zen response, and the
    native mock sees nothing."""
    port, httpd = _live_proxy(backend)
    try:
        with _cleared_key_env(), mock.patch.dict(
            os.environ,
            {
                passthrough.NATIVE_BASE_URL_ENV: f"{backend}{NATIVE_BASE_PATH}",
                passthrough.NATIVE_INSECURE_ENV: "1",
                "OPENCODE_GO_PROXY_MAX_RETRIES": "1",
            },
        ):
            resp = _post(
                port,
                {"model": "zen/deepseek-v4-flash-free", "input": "hi"},
                auth=CLIENT_AUTH,
            )
    finally:
        httpd.shutdown()
        httpd.server_close()

    assert NATIVE_TEXT not in resp.raw.decode("utf-8", "replace")
    native, go, _zen = _partition_captured()
    assert native == [] and go == []
    assert resp.status in (200, 401, 403, 429)
    if resp.status == 429:
        body = json.loads(resp.raw)
        assert "error" in body and "type" in body["error"]

