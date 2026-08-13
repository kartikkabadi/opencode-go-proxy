import io
import json
import os
import tarfile
import urllib.error
import urllib.request
from unittest import mock

from opencode_go_proxy import ops


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


def _http_error(code: int, body: str = ""):
    return urllib.error.HTTPError("https://up.test/v1/chat/completions", code, "err", {}, io.BytesIO(body.encode()))


class TestApiKey:
    def test_missing(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("OPENCODE_GO_API_KEY", None)
            c = ops.check_api_key()
        assert not c.ok

    def test_present(self) -> None:
        with mock.patch.dict(os.environ, {"OPENCODE_GO_API_KEY": "sk-abc123"}):
            assert ops.check_api_key().ok


class TestService:
    def test_ok(self) -> None:
        with mock.patch("opencode_go_proxy.ops.urllib.request.urlopen", return_value=MockResp(b'{"status": "ok"}')):
            c = ops.check_service("http://127.0.0.1:1/health")
        assert c.ok

    def test_down(self) -> None:
        with mock.patch("opencode_go_proxy.ops.urllib.request.urlopen",
                        side_effect=urllib.error.URLError("refused")):
            c = ops.check_service("http://127.0.0.1:1/health")
        assert not c.ok


class TestMeter:
    def test_writable(self, tmp_path) -> None:
        with mock.patch.dict(os.environ, {"OPENCODE_GO_PROXY_STATE_DIR": str(tmp_path)}):
            c = ops.check_meter()
        assert c.ok
        assert os.path.exists(os.path.join(str(tmp_path), "usage-events.jsonl"))


class TestLogs:
    def test_present(self, tmp_path) -> None:
        (tmp_path / "opencode-go-proxy.log").write_text("x\n")
        with mock.patch.object(ops, "LOG_DIR", str(tmp_path)):
            c = ops.check_logs()
        assert c.ok


class TestConfig:
    def test_points_at_proxy(self, tmp_path) -> None:
        p = tmp_path / "config.toml"
        p.write_text('openai_base_url = "http://127.0.0.1:8787/v1"\n')
        with mock.patch.object(ops, "CONFIG_PATH", str(p)):
            assert ops.check_config().ok

    def test_not_pointing(self, tmp_path) -> None:
        p = tmp_path / "config.toml"
        p.write_text('openai_base_url = "https://api.openai.com/v1"\n')
        with mock.patch.object(ops, "CONFIG_PATH", str(p)):
            assert not ops.check_config().ok


class TestDoctor:
    def test_json_all_ok(self) -> None:
        with mock.patch.object(ops, "check_api_key", return_value=ops.Check("api key", True)), \
             mock.patch.object(ops, "check_service", return_value=ops.Check("service", True)), \
             mock.patch.object(ops, "check_meter", return_value=ops.Check("meter", True)), \
             mock.patch.object(ops, "check_logs", return_value=ops.Check("logs", True)), \
             mock.patch.object(ops, "check_config", return_value=ops.Check("config", True)):
            rc = ops.doctor(["--json"])
        assert rc == 0

    def test_json_failure(self) -> None:
        with mock.patch.object(ops, "check_api_key", return_value=ops.Check("api key", False, hint="fix it")), \
             mock.patch.object(ops, "check_service", return_value=ops.Check("service", True)), \
             mock.patch.object(ops, "check_meter", return_value=ops.Check("meter", True)), \
             mock.patch.object(ops, "check_logs", return_value=ops.Check("logs", True)), \
             mock.patch.object(ops, "check_config", return_value=ops.Check("config", True)):
            rc = ops.doctor(["--json"])
        assert rc == 1


class TestSmoke:
    def test_ok(self) -> None:
        body = json.dumps({
            "model": "deepseek-v4-flash",
            "choices": [{"message": {"content": "pong"}}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 1},
        }).encode()
        with mock.patch.dict(os.environ, {"OPENCODE_GO_API_KEY": "sk-x"}), \
             mock.patch("opencode_go_proxy.ops.urllib.request.urlopen", return_value=MockResp(body)):
            assert ops.smoke_test() == 0

    def test_http_error(self) -> None:
        with mock.patch.dict(os.environ, {"OPENCODE_GO_API_KEY": "sk-x"}), \
             mock.patch("opencode_go_proxy.ops.urllib.request.urlopen", side_effect=_http_error(503)):
            assert ops.smoke_test() == 1

    def test_missing_key(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("OPENCODE_GO_API_KEY", None)
            assert ops.smoke_test() == 1


class TestMask:
    def test_redacts_secrets(self) -> None:
        assert ops._mask_secret_values('api_key = "sk-live"') == "api_key= ***redacted***"
        assert ops._mask_secret_values('model = "deepseek-v4-flash"') == 'model = "deepseek-v4-flash"'


class TestSupportBundle:
    def test_bundle_contains_files_and_redacts(self, tmp_path) -> None:
        cfg = tmp_path / "config.toml"
        cfg.write_text('openai_base_url = "http://127.0.0.1:8787/v1"\napi_key = "sk-secret-value"\n')
        log_dir = tmp_path / "logs"
        log_dir.mkdir()
        (log_dir / "opencode-go-proxy.err").write_text("2026-08-10 server.start\n")
        out = tmp_path / "bundle.tar.gz"
        with mock.patch.object(ops, "LOG_DIR", str(log_dir)), \
             mock.patch.object(ops, "CONFIG_PATH", str(cfg)), \
             mock.patch.dict(os.environ, {"OPENCODE_GO_PROXY_STATE_DIR": str(tmp_path / "state")}):
            assert ops.support_bundle(["--output", str(out)]) == 0
        names = []
        with tarfile.open(out, "r:gz") as tf:
            for member in tf.getmembers():
                names.append(member.name)
                if member.name == "opencode-go/config.toml":
                    text = tf.extractfile(member).read().decode()
                    assert "sk-secret-value" not in text
                    assert "***redacted***" in text
        assert "opencode-go/version.json" in names
        assert "opencode-go/opencode-go-proxy.err" in names
        assert "opencode-go/usage-events.jsonl" in names
