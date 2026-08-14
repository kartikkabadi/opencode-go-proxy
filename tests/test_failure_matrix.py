"""Failure-mode matrix: proxy reliability contracts against a scripted mock upstream.

Every scenario drives the real proxy HTTP surface (in-process server on the
scratch port 8792) against an http.server mock upstream scripted per scenario,
then verifies the client-visible response, the usage-meter row, and the mock's
request log. Offline and deterministic: all traffic is loopback, the state dir
is a per-test tmp dir, and the proxy env carries an explicit fake key
("test-key") so key resolution never touches the environment or the macOS
keychain. The keyless scenario clears the key itself and forces keychain
failure to prove the 401 contract.

Contract expectations come from the reliability spec; where the current code
behaves differently the scenario FAILS and reports the mismatch with evidence
instead of silently adjusting the expectation.
"""

import json
import os
import socket
import struct
import subprocess
import threading
import time
from collections.abc import Callable, Generator
from http.client import HTTPConnection, HTTPResponse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

import pytest

from opencode_go_proxy import catalog
from opencode_go_proxy.app import ProxyConfig, ResponsesProxyHandler
from opencode_go_proxy.meter import usage_events_path
from opencode_go_proxy.secrets import clear_api_key_cache, resolve_api_key
from opencode_go_proxy.usage_poller import clear_cache as clear_usage_poller_cache

SCRATCH_PORT_PREFERRED = 8792
PROXY_TIMEOUT_SEC = 2.0
# Must exceed PROXY_TIMEOUT_SEC * 3 attempts plus backoff so the proxy always
# times out client-side instead of receiving a late response.
HANG_SLEEP_SEC = 20.0
MOCK_SETTLE_SEC = 0.2  # let the proxy consume the committed chunk before the RST lands
STREAM_CHUNK_COUNT = 40
STREAM_CHUNK_DELAY_SEC = 0.05  # spread chunks so the test can cancel mid-stream
MODEL = "deepseek-v4-flash"

MODE_401 = "401"
MODE_429 = "429"
MODE_500 = "500"
MODE_HANG = "hang"
MODE_ABORT = "abort"
MODE_EMPTY = "empty"
MODE_STREAM = "stream"
MODE_GARBAGE = "garbage"
MODE_OK = "ok"

RETRY_AFTER_VALUE = "2"


def _sse_chunk(text: str) -> bytes:
    chunk = {
        "id": "chatcmpl-matrix",
        "object": "chat.completion.chunk",
        "model": MODEL,
        "choices": [{"index": 0, "delta": {"content": text}}],
    }
    return b"data: " + json.dumps(chunk, separators=(",", ":")).encode("utf-8") + b"\n\n"


def _failed_keychain_run(*args, **kwargs) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=args, returncode=1, stdout="")


class RequestRecord:
    """What the mock saw for one upstream request.

    The authorization value is stored for in-memory comparison only and never
    rendered into output or failure messages.
    """

    def __init__(self, path: str, headers: dict[str, str], raw_body: bytes) -> None:
        self.path = path
        # http.client title-cases header names on the wire; normalize for lookups.
        self.headers = {name.lower(): value for name, value in headers.items()}
        self.content_type = self.headers.get("content-type", "")
        self.has_authorization = "authorization" in self.headers
        authorization = self.headers.get("authorization", "")
        self.authorization_is_bearer = authorization.startswith("Bearer ")
        self.authorization_value = authorization
        self.raw_body = raw_body


class ScriptedUpstream:
    """One scripted persona: records every request, then answers per `mode`."""

    def __init__(self, mode: str) -> None:
        self.mode = mode
        self.requests: list[RequestRecord] = []
        self.notes: list[str] = []

    def record(self, path: str, headers: dict[str, str], raw_body: bytes) -> None:
        self.requests.append(RequestRecord(path, headers, raw_body))

    def note(self, message: str) -> None:
        self.notes.append(message)

    def reset_log(self) -> None:
        self.requests.clear()
        self.notes.clear()

    @property
    def count(self) -> int:
        return len(self.requests)

    def handle(self, handler: BaseHTTPRequestHandler) -> None:
        handlers = {
            MODE_401: self._respond_401,
            MODE_429: self._respond_429,
            MODE_500: self._respond_500,
            MODE_HANG: self._respond_hang,
            MODE_ABORT: self._respond_abort,
            MODE_EMPTY: self._respond_empty,
            MODE_STREAM: self._respond_stream,
            MODE_GARBAGE: self._respond_garbage,
            MODE_OK: self._respond_ok,
        }
        handlers[self.mode](handler)

    def _send_json(
        self,
        handler: BaseHTTPRequestHandler,
        status: int,
        payload: dict,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        handler.send_response(status)
        for name, value in (extra_headers or {}).items():
            handler.send_header(name, value)
        handler.send_header("content-type", "application/json")
        handler.send_header("content-length", str(len(raw)))
        handler.end_headers()
        handler.wfile.write(raw)

    def _send_sse_head(self, handler: BaseHTTPRequestHandler) -> None:
        handler.send_response(200)
        handler.send_header("content-type", "text/event-stream")
        handler.end_headers()

    def _respond_401(self, handler: BaseHTTPRequestHandler) -> None:
        self._send_json(
            handler,
            401,
            {"error": {"type": "authentication_error", "message": "upstream rejects the key"}},
        )

    def _respond_429(self, handler: BaseHTTPRequestHandler) -> None:
        self._send_json(
            handler,
            429,
            {"error": {"type": "rate_limit_error", "message": "slow down"}},
            {"retry-after": RETRY_AFTER_VALUE},
        )

    def _respond_500(self, handler: BaseHTTPRequestHandler) -> None:
        self._send_json(handler, 500, {"error": {"type": "server_error", "message": "boom"}})

    def _respond_hang(self, handler: BaseHTTPRequestHandler) -> None:
        time.sleep(HANG_SLEEP_SEC)
        self.note("upstream woke from hang sleep after the proxy timed out")
        self._send_json(
            handler,
            200,
            {"id": "chatcmpl-late", "object": "chat.completion", "model": MODEL, "choices": []},
        )

    def _respond_abort(self, handler: BaseHTTPRequestHandler) -> None:
        self._send_sse_head(handler)
        handler.wfile.write(_sse_chunk("hel"))
        handler.wfile.flush()
        time.sleep(MOCK_SETTLE_SEC)
        handler.connection.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        handler.connection.close()

    def _respond_empty(self, handler: BaseHTTPRequestHandler) -> None:
        self._send_sse_head(handler)
        handler.wfile.write(b"data: [DONE]\n\n")
        handler.wfile.flush()

    def _respond_stream(self, handler: BaseHTTPRequestHandler) -> None:
        self._send_sse_head(handler)
        for index in range(STREAM_CHUNK_COUNT):
            handler.wfile.write(_sse_chunk(f"chunk-{index}"))
            handler.wfile.flush()
            time.sleep(STREAM_CHUNK_DELAY_SEC)
        handler.wfile.write(b"data: [DONE]\n\n")
        handler.wfile.flush()

    def _respond_garbage(self, handler: BaseHTTPRequestHandler) -> None:
        self._send_sse_head(handler)
        handler.wfile.write(b"\x00\xffthis is not SSE\r\nno data prefix here\r\n")
        handler.wfile.flush()

    def _respond_ok(self, handler: BaseHTTPRequestHandler) -> None:
        self._send_json(
            handler,
            200,
            {
                "id": "chatcmpl-matrix",
                "object": "chat.completion",
                "model": MODEL,
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "ok"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            },
        )


class MockUpstreamHandler(BaseHTTPRequestHandler):
    """HTTP surface for the scripted upstream; every request is recorded."""

    protocol_version = "HTTP/1.0"
    server_version = "MockUpstream/1.0"

    def log_message(self, message_format: str, *args: object) -> None:
        # The mock's access log is test noise; ScriptedUpstream holds the real log.
        return

    def do_POST(self) -> None:
        length = int(self.headers.get("content-length") or "0")
        raw_body = self.rfile.read(length) if length else b""
        self.server.behavior.record(self.path, dict(self.headers), raw_body)
        try:
            self.server.behavior.handle(self)
        except (BrokenPipeError, ConnectionResetError):
            self.server.behavior.note("proxy abandoned the connection mid-response")


class _MockServer(ThreadingHTTPServer):
    daemon_threads = True
    behavior: ScriptedUpstream

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.behavior = ScriptedUpstream(MODE_OK)


class _ScratchProxyServer(ThreadingHTTPServer):
    daemon_threads = True
    config: ProxyConfig


def _wait_for_listener(port: int, timeout_sec: float = 5.0) -> None:
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return
        except OSError:
            time.sleep(0.01)
    raise AssertionError(f"port {port} never accepted connections")


def _pick_scratch_port() -> int:
    """A free scratch port, preferring 8792 (never the live 8787).

    Parallel verification agents share the 8791-8799 range, so a sibling may
    hold the preferred port; fall back to any free scratch port, then to an
    OS-assigned ephemeral port.
    """
    candidates = [SCRATCH_PORT_PREFERRED, *range(SCRATCH_PORT_PREFERRED + 1, SCRATCH_PORT_PREFERRED + 8)]
    for port in candidates:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            try:
                probe.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@pytest.fixture
def scratch_port() -> Generator[int, None, None]:
    yield _pick_scratch_port()


@pytest.fixture
def mock_upstream() -> Generator[_MockServer, None, None]:
    server = _MockServer(("127.0.0.1", 0), MockUpstreamHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    _wait_for_listener(server.server_address[1])
    yield server
    server.shutdown()
    server.server_close()


@pytest.fixture
def proxy_env(mock_upstream: _MockServer) -> Generator[str, None, None]:
    """Scratch-proxy env: explicit fake key, loopback-only, fast retries.

    OPENCODE_GO_API_KEY is set to a literal fake key so key resolution is
    deterministic and never consults the ambient env or the macOS keychain
    (CI has neither). OPENCODE_API_KEY is pinned empty so an inherited
    standard key cannot leak in either.
    """
    base_url = f"http://127.0.0.1:{mock_upstream.server_address[1]}"
    with mock.patch.dict(
        os.environ,
        {
            "OPENCODE_GO_API_KEY": "test-key",
            "OPENCODE_API_KEY": "",
            "OPENCODE_GO_PROXY_RETRY_BASE_MS": "1",
            "OPENCODE_GO_PROXY_MAX_RETRIES": "2",
            "OPENCODE_GO_USAGE_URL": f"{base_url}/usage",
            "HTTP_PROXY": "",
            "HTTPS_PROXY": "",
            "ALL_PROXY": "",
            "http_proxy": "",
            "https_proxy": "",
            "all_proxy": "",
            "SSL_CERT_FILE": "",
            "SSL_CERT_DIR": "",
            "NO_PROXY": "*",
            "no_proxy": "*",
        }
    ):
        clear_api_key_cache()
        clear_usage_poller_cache()
        yield base_url


def _start_scratch_proxy(chat_base_url: str, port: int) -> _ScratchProxyServer:
    server = _ScratchProxyServer(("127.0.0.1", port), ResponsesProxyHandler)
    server.config = ProxyConfig(
        bind="127.0.0.1",
        port=port,
        chat_base_url=chat_base_url,
        api_key_env="OPENCODE_GO_API_KEY",
        timeout_sec=PROXY_TIMEOUT_SEC,
        max_body_bytes=20 * 1024 * 1024,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    _wait_for_listener(port)
    return server


@pytest.fixture
def proxy(proxy_env: str, scratch_port: int) -> Generator[_ScratchProxyServer, None, None]:
    server = _start_scratch_proxy(proxy_env, scratch_port)
    yield server
    server.shutdown()
    server.server_close()


def responses_payload(*, stream: bool = False) -> dict:
    return {"model": MODEL, "input": "Say something.", "stream": stream}


def chat_payload() -> dict:
    return {"model": MODEL, "messages": [{"role": "user", "content": "hi"}]}


def http_request(
    port: int,
    method: str,
    path: str,
    *,
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
    timeout_sec: float = 15.0,
) -> tuple[int, dict[str, str], bytes]:
    """One round trip against the scratch proxy; the connection is always closed."""
    conn = HTTPConnection("127.0.0.1", port, timeout=timeout_sec)
    try:
        conn.request(method, path, body, headers or {})
        response = conn.getresponse()
        raw = response.read()
        status = response.status
        response_headers = {name.lower(): value for name, value in response.getheaders()}
        return status, response_headers, raw
    finally:
        conn.close()


def post_json(port: int, path: str, payload: dict) -> tuple[int, dict[str, str], bytes]:
    raw = json.dumps(payload).encode("utf-8")
    return http_request(port, "POST", path, body=raw, headers={"content-type": "application/json"})


def open_stream(
    port: int, path: str, payload: dict, *, timeout_sec: float = 15.0
) -> tuple[HTTPConnection, HTTPResponse]:
    conn = HTTPConnection("127.0.0.1", port, timeout=timeout_sec)
    conn.request("POST", path, json.dumps(payload).encode("utf-8"), {"content-type": "application/json"})
    response = conn.getresponse()
    return conn, response


def read_until(response: HTTPResponse, marker: bytes, *, timeout_sec: float = 15.0) -> bytes:
    """Read the SSE body until `marker` appears or the upstream closes the stream."""
    accumulated = bytearray()
    deadline = time.monotonic() + timeout_sec
    while marker not in accumulated:
        if time.monotonic() > deadline:
            raise AssertionError(f"stream did not reach {marker!r} within {timeout_sec}s")
        chunk = response.read1(8192)
        if not chunk:
            break
        accumulated.extend(chunk)
    return bytes(accumulated)


def read_until_then_disconnect(
    conn: HTTPConnection, response: HTTPResponse, marker: bytes, *, timeout_sec: float = 15.0
) -> bytes:
    """Read until `marker`, then cancel the stream as a real client would.

    The response must be closed, not just the connection: HTTPConnection.close()
    only marks the socket closed in Python while the response's buffered reader
    keeps the FD open, so the peer would never see a FIN/RST and the proxy's
    liveness peek could not detect the cancel. Closing the response sends the
    FIN (or RST with unread data) that the peek reads as the client-gone
    signal the meter must record as status 0.
    """
    accumulated = read_until(response, marker, timeout_sec=timeout_sec)
    response.close()
    conn.close()
    return accumulated


def meter_events() -> list[dict]:
    try:
        with open(usage_events_path(), encoding="utf-8") as handle:
            return [json.loads(line) for line in handle if line.strip()]
    except OSError:
        return []


def wait_for_event(predicate: Callable[[dict], bool], *, timeout_sec: float = 10.0) -> dict:
    """Poll the meter file for an event matching `predicate` (async stream writes)."""
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        for event in meter_events():
            if predicate(event):
                return event
        time.sleep(0.05)
    raise AssertionError(f"no meter event matched within {timeout_sec}s; saw {meter_events()}")


class ExpectationCheck:
    """Collect scenario contract checks so one run reports every mismatch."""

    def __init__(self, scenario: str) -> None:
        self.scenario = scenario
        self.failures: list[str] = []

    def check(self, label: str, condition: bool, actual: object) -> None:
        if not condition:
            self.failures.append(f"{label}: got {actual!r}")

    def expect_equal(self, label: str, actual: object, expected: object) -> None:
        if actual != expected:
            self.failures.append(f"{label}: expected {expected!r}, got {actual!r}")

    def finish(self) -> None:
        if self.failures:
            joined = "; ".join(self.failures)
            raise AssertionError(f"{self.scenario}: {len(self.failures)} mismatch(es): {joined}")


def test_scenario_01_upstream_401_relayed_verbatim(
    proxy: _ScratchProxyServer, mock_upstream: _MockServer, scratch_port: int
) -> None:
    mock_upstream.behavior.mode = MODE_401
    check = ExpectationCheck("scenario 01: 401 passthrough")
    status, _headers, raw = post_json(scratch_port, "/chat/completions", chat_payload())
    body = json.loads(raw)
    check.expect_equal("client status", status, 401)
    check.expect_equal("upstream error type relayed", body.get("error", {}).get("type"), "authentication_error")
    check.expect_equal("upstream error message relayed", body.get("error", {}).get("message"), "upstream rejects the key")
    check.expect_equal("upstream saw exactly one request", mock_upstream.behavior.count, 1)
    first = mock_upstream.behavior.requests[0]
    check.check(
        "client auth forwarded as a bearer token",
        first.has_authorization and first.authorization_is_bearer,
        (first.has_authorization, first.authorization_is_bearer),
    )
    resolved_key = resolve_api_key(proxy.config, "matrix-check")
    check.check(
        "upstream received exactly the resolved key",
        first.authorization_value == f"Bearer {resolved_key}",
        first.authorization_value == f"Bearer {resolved_key}",
    )
    check.check("upstream got a JSON body", first.content_type.startswith("application/json"), first.content_type)
    events = meter_events()
    check.expect_equal("one meter row", len(events), 1)
    check.expect_equal("meter status 401", events[-1].get("status"), 401)
    check.check("no retries recorded for a permanent error", "retries" not in events[-1], events[-1])
    check.finish()


def test_scenario_02_429_retries_then_surfaces_retry_after(
    proxy: _ScratchProxyServer, mock_upstream: _MockServer, scratch_port: int
) -> None:
    mock_upstream.behavior.mode = MODE_429
    check = ExpectationCheck("scenario 02: 429 + retry-after")
    status, headers, raw = post_json(scratch_port, "/v1/responses", responses_payload())
    body = json.loads(raw)
    message = body.get("error", {}).get("message", "")
    check.expect_equal("client status", status, 429)
    check.check("retry-after value surfaced in message", f"retry after {RETRY_AFTER_VALUE}s" in message, message)
    check.check("retry-after header forwarded to client", "retry-after" in headers, headers.get("retry-after"))
    check.expect_equal("upstream saw initial + 2 retries", mock_upstream.behavior.count, 3)
    events = meter_events()
    check.expect_equal("one meter row for the responses turn", len(events), 1)
    check.expect_equal("meter status 429", events[-1].get("status"), 429)
    check.expect_equal("meter counts the 2 retries", events[-1].get("retries"), 2)

    mock_upstream.behavior.reset_log()
    status, headers, raw = post_json(scratch_port, "/chat/completions", chat_payload())
    check.expect_equal("verbatim relay keeps upstream status 429", status, 429)
    check.expect_equal(
        "verbatim relay keeps the upstream body",
        json.loads(raw).get("error", {}).get("message"),
        "slow down",
    )
    check.check("verbatim relay forwards retry-after header", "retry-after" in headers, headers.get("retry-after"))
    check.expect_equal("verbatim relay also retried twice", mock_upstream.behavior.count, 3)
    events = meter_events()
    check.expect_equal("second meter row for the verbatim turn", len(events), 2)
    check.expect_equal("verbatim meter status 429", events[-1].get("status"), 429)
    check.expect_equal("verbatim meter counts the 2 retries", events[-1].get("retries"), 2)
    check.finish()


def test_scenario_03_500_retried_then_surfaces_502(
    proxy: _ScratchProxyServer, mock_upstream: _MockServer, scratch_port: int
) -> None:
    mock_upstream.behavior.mode = MODE_500
    check = ExpectationCheck("scenario 03: 500 retry")
    status, _headers, raw = post_json(scratch_port, "/v1/responses", responses_payload())
    body = json.loads(raw)
    check.expect_equal("client status", status, 502)
    check.check(
        "proxy envelope names the upstream 500",
        "upstream HTTP 500" in body.get("error", {}).get("message", ""),
        body.get("error", {}).get("message", ""),
    )
    check.expect_equal("upstream saw initial + 2 retries", mock_upstream.behavior.count, 3)
    event = meter_events()[-1]
    check.expect_equal("meter status reflects the final 502", event.get("status"), 502)
    check.expect_equal("meter counts the 2 retries", event.get("retries"), 2)
    check.finish()


def test_scenario_04_hang_times_out_504(
    proxy: _ScratchProxyServer, mock_upstream: _MockServer, scratch_port: int
) -> None:
    mock_upstream.behavior.mode = MODE_HANG
    check = ExpectationCheck("scenario 04: upstream hang")
    started = time.monotonic()
    status, _headers, raw = post_json(scratch_port, "/v1/responses", responses_payload())
    elapsed = time.monotonic() - started
    body = json.loads(raw)
    check.expect_equal("client status", status, 504)
    check.check(
        "timeout message",
        "timeout" in body.get("error", {}).get("message", ""),
        body.get("error", {}).get("message", ""),
    )
    check.check("no hang: answered within 3 timeouts + slack", elapsed < 15.0, f"{elapsed:.1f}s")
    check.expect_equal("upstream saw initial + 2 retries", mock_upstream.behavior.count, 3)
    event = meter_events()[-1]
    check.expect_equal("meter status 504", event.get("status"), 504)
    check.expect_equal("meter counts the 2 retries", event.get("retries"), 2)
    check.finish()


def test_scenario_05_mid_stream_death_marks_aborted(
    proxy: _ScratchProxyServer, mock_upstream: _MockServer, scratch_port: int
) -> None:
    mock_upstream.behavior.mode = MODE_ABORT
    check = ExpectationCheck("scenario 05: mid-stream death after 200 head")
    conn, response = open_stream(scratch_port, "/v1/responses", responses_payload(stream=True))
    try:
        check.expect_equal("200 head committed before upstream died", response.status, 200)
        check.expect_equal("SSE content type", response.getheader("content-type", ""), "text/event-stream")
        raw = read_until(response, b"[DONE]")
    finally:
        conn.close()
    text = raw.decode("utf-8", errors="replace")
    check.check("terminal error event sent", "response.error" in text, text[-300:])
    check.check("abort message", "upstream stream aborted" in text, text[-300:])
    check.check("no success claim", "response.completed" not in text, "response.completed present")
    check.check("client saw the committed delta before the error", "output_text.delta" in text, text[:300])
    check.expect_equal("upstream saw exactly one request", mock_upstream.behavior.count, 1)
    event = wait_for_event(lambda candidate: candidate.get("status") == 502 and candidate.get("streamAborted") is True)
    check.expect_equal("meter status 502", event.get("status"), 502)
    check.expect_equal("meter marks stream aborted", event.get("streamAborted"), True)
    check.expect_equal("one meter row", len(meter_events()), 1)
    check.finish()


def test_scenario_06_empty_completion_retried_once(
    proxy: _ScratchProxyServer, mock_upstream: _MockServer, scratch_port: int
) -> None:
    mock_upstream.behavior.mode = MODE_EMPTY
    check = ExpectationCheck("scenario 06: empty completion")
    conn, response = open_stream(scratch_port, "/v1/responses", responses_payload(stream=True))
    try:
        raw = read_until(response, b"[DONE]")
    finally:
        conn.close()
    text = raw.decode("utf-8", errors="replace")
    check.check("empty-completion error surfaced", "response.error" in text and "empty_completion" in text, text[-300:])
    check.check("no success claim", "response.completed" not in text, "response.completed present")
    check.expect_equal("first empty attempt retried once", mock_upstream.behavior.count, 2)
    event = wait_for_event(lambda candidate: candidate.get("emptyCompletion") is True)
    check.expect_equal("meter marks empty completion", event.get("emptyCompletion"), True)
    check.expect_equal("meter counts the empty retry", event.get("retries"), 1)
    check.expect_equal("meter status is 502 (failed-turn contract)", event.get("status"), 502)
    check.finish()


def test_scenario_07_client_cancel_meters_zero(
    proxy: _ScratchProxyServer, mock_upstream: _MockServer, scratch_port: int
) -> None:
    mock_upstream.behavior.mode = MODE_STREAM
    check = ExpectationCheck("scenario 07: client cancel mid-stream")
    conn, response = open_stream(scratch_port, "/v1/responses", responses_payload(stream=True))
    try:
        check.expect_equal("stream starts with a 200 head", response.status, 200)
        raw = read_until_then_disconnect(conn, response, b"output_text.delta")
        check.check(
            "client saw streamed deltas before cancelling",
            b"response.created" in raw and b"output_text.delta" in raw,
            raw[:200],
        )
    finally:
        conn.close()
    event = wait_for_event(lambda candidate: candidate.get("status") == 0, timeout_sec=6.0)
    check.expect_equal("meter status 0 for client cancel", event.get("status"), 0)
    check.expect_equal("meter marks the stream aborted", event.get("streamAborted"), True)
    check.expect_equal("exactly one meter row", len(meter_events()), 1)
    check.finish()


def test_scenario_08_garbage_sse_surfaces_error(
    proxy: _ScratchProxyServer, mock_upstream: _MockServer, scratch_port: int
) -> None:
    mock_upstream.behavior.mode = MODE_GARBAGE
    check = ExpectationCheck("scenario 08: garbage SSE")
    started = time.monotonic()
    conn, response = open_stream(scratch_port, "/v1/responses", responses_payload(stream=True))
    try:
        raw = read_until(response, b"[DONE]")
    finally:
        conn.close()
    elapsed = time.monotonic() - started
    text = raw.decode("utf-8", errors="replace")
    check.check("error surfaced", "response.error" in text, text[-300:])
    check.check("no hang", elapsed < 15.0, f"{elapsed:.1f}s")
    check.expect_equal("upstream saw exactly one request", mock_upstream.behavior.count, 1)
    event = wait_for_event(lambda candidate: candidate.get("status") == 502)
    check.expect_equal("meter status 502 for unparseable stream", event.get("status"), 502)
    check.check("no streamAborted claim (nothing was streamed)", event.get("streamAborted") is not True, event)
    check.finish()


def test_scenario_09_upstream_down_degrades_cleanly(
    proxy: _ScratchProxyServer, mock_upstream: _MockServer, scratch_port: int
) -> None:
    mock_upstream.shutdown()
    mock_upstream.server_close()
    check = ExpectationCheck("scenario 09: upstream down")
    status, _headers, _raw = http_request(scratch_port, "GET", "/health")
    check.expect_equal("health 200 while upstream is down", status, 200)
    status, _headers, raw = http_request(scratch_port, "GET", "/state")
    check.expect_equal("state 200 while upstream is down", status, 200)
    usage = json.loads(raw).get("usage", {})
    check.check("usage degrades to zeros/null", usage.get("todayTurns") == 0 and usage.get("go") is None, usage)
    status, _headers, raw = http_request(scratch_port, "GET", "/v1/models")
    check.expect_equal("models 200 from the seed cache", status, 200)
    ids = [entry.get("id") for entry in json.loads(raw).get("data", [])]
    check.check("seed fallback non-empty", len(ids) > 0, ids)
    status, _headers, raw = post_json(scratch_port, "/v1/responses", responses_payload())
    body = json.loads(raw)
    check.expect_equal("post surfaces 502 cleanly", status, 502)
    check.check("proxy error envelope", "error" in body and bool(body["error"].get("message")), body)
    event = meter_events()[-1]
    check.expect_equal("meter status 502", event.get("status"), 502)
    check.expect_equal("meter counts the 2 retries", event.get("retries"), 2)
    status, _headers, _raw = http_request(scratch_port, "GET", "/health")
    check.expect_equal("health still 200 after the failed post", status, 200)
    check.finish()


def test_scenario_10_keyless_returns_clean_error(
    proxy: _ScratchProxyServer, mock_upstream: _MockServer, scratch_port: int
) -> None:
    check = ExpectationCheck("scenario 10: keyless")
    # Keyless by design: override the fixture's fake key with empty values and
    # force keychain lookup to fail, proving the proxy rejects with 401 before
    # any upstream contact.
    with (
        mock.patch.dict(os.environ, {"OPENCODE_GO_API_KEY": "", "OPENCODE_API_KEY": ""}),
        mock.patch("opencode_go_proxy.secrets.subprocess.run", side_effect=_failed_keychain_run),
    ):
        clear_api_key_cache()
        status, _headers, raw = post_json(scratch_port, "/v1/responses", responses_payload())
        body = json.loads(raw)
        check.expect_equal("client status", status, 401)
        check.check(
            "clear missing-key message",
            "OPENCODE_GO_API_KEY" in body.get("error", {}).get("message", ""),
            body.get("error", {}).get("message", ""),
        )
        check.expect_equal("upstream never contacted", mock_upstream.behavior.count, 0)
        events = meter_events()
        check.expect_equal("one meter row", len(events), 1)
        check.expect_equal("meter status 401", events[-1].get("status"), 401)
        status, _headers, _raw = http_request(scratch_port, "GET", "/health")
        check.expect_equal("health still 200", status, 200)
    check.finish()


def test_scenario_11_corrupt_catalog_recovers(proxy_env: str, scratch_port: int) -> None:
    merged_path = catalog.merged_models_path()
    with open(merged_path, "w", encoding="utf-8") as handle:
        handle.write("this is not json {{{")
    server = _start_scratch_proxy(proxy_env, scratch_port)
    try:
        check = ExpectationCheck("scenario 11: corrupt catalog")
        status, _headers, _raw = http_request(scratch_port, "GET", "/health")
        check.expect_equal("health 200 with corrupt catalog", status, 200)
        status, _headers, raw = http_request(scratch_port, "GET", "/v1/models")
        check.expect_equal("models endpoint 200", status, 200)
        ids = [entry.get("id") for entry in json.loads(raw).get("data", [])]
        check.check("models fall back to the seed set", len(ids) > 0, ids)
        catalog.render_merged_catalog()
        with open(merged_path, encoding="utf-8") as handle:
            recovered = json.load(handle)
        check.check(
            "refresh rewrites valid JSON with a models list",
            isinstance(recovered.get("models"), list),
            type(recovered.get("models")).__name__,
        )
        status, _headers, raw = http_request(scratch_port, "GET", "/v1/models")
        check.expect_equal("models endpoint 200 after refresh", status, 200)
        ids = [entry.get("id") for entry in json.loads(raw).get("data", [])]
        check.check("models still served after refresh", len(ids) > 0, ids)
        check.finish()
    finally:
        server.shutdown()
        server.server_close()
