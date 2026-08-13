import io
import json
import os
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
    return urllib.error.HTTPError("https://up.test/v1/responses", code, "err", {}, io.BytesIO(body.encode()))


class TestApiKey:
    def test_missing(self) -> None:
        with mock.patch.object(ops.secrets, "api_key_source", return_value=None):
            c = ops.check_api_key()
        assert not c.ok
        assert c.status == "fail"

    def test_resolved_from_env(self) -> None:
        with mock.patch.object(ops.secrets, "api_key_source", return_value="env:OPENCODE_GO_API_KEY"):
            c = ops.check_api_key()
        assert c.ok
        assert "env:OPENCODE_GO_API_KEY" in c.detail

    def test_resolved_from_keychain(self) -> None:
        with mock.patch.object(ops.secrets, "api_key_source", return_value="keychain:opencode-go-api-key"):
            c = ops.check_api_key()
        assert c.ok
        assert "keychain:opencode-go-api-key" in c.detail


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


class TestPort:
    def test_owned_by_proxy(self) -> None:
        with mock.patch("opencode_go_proxy.ops.urllib.request.urlopen", return_value=MockResp(b'{"status": "ok"}')):
            c = ops.check_port("http://127.0.0.1:1/health")
        assert c.ok
        assert "owned by proxy" in c.detail

    def test_free(self) -> None:
        with mock.patch("opencode_go_proxy.ops.urllib.request.urlopen",
                        side_effect=urllib.error.URLError("refused")):
            c = ops.check_port("http://127.0.0.1:1/health")
        assert c.ok
        assert "free" in c.detail

    def test_foreign_listener_warns(self) -> None:
        with mock.patch("opencode_go_proxy.ops.urllib.request.urlopen", side_effect=_http_error(404)):
            c = ops.check_port("http://127.0.0.1:1/health")
        assert not c.ok
        assert c.status == "warn"


class TestMeter:
    def test_writable(self, tmp_path) -> None:
        with mock.patch.dict(os.environ, {"OPENCODE_GO_PROXY_STATE_DIR": str(tmp_path)}):
            c = ops.check_meter()
        assert c.ok
        assert os.path.exists(os.path.join(str(tmp_path), "usage-events.jsonl"))

    def test_read_only_does_not_record(self, tmp_path) -> None:
        state = tmp_path / "state"
        state.mkdir(exist_ok=True)
        p = state / "usage-events.jsonl"
        p.write_text('{"model":"deepseek-v4-flash","status":0}\n')
        before = p.read_text()
        with mock.patch.dict(os.environ, {"OPENCODE_GO_PROXY_STATE_DIR": str(state)}):
            c = ops.check_meter()
        assert c.ok
        assert p.read_text() == before


class TestLogs:
    def test_present(self, tmp_path) -> None:
        (tmp_path / "opencode-go-proxy.log").write_text("x\n")
        with mock.patch.object(ops, "LOG_DIR", str(tmp_path)):
            c = ops.check_logs()
        assert c.ok
        assert "opencode-go-proxy.log" in c.detail

    def test_writable_without_files(self, tmp_path) -> None:
        with mock.patch.object(ops, "LOG_DIR", str(tmp_path)):
            c = ops.check_logs()
        assert c.ok
        assert not os.path.exists(os.path.join(str(tmp_path), f".doctor-probe-{os.getpid()}"))


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

    def test_config_file_presence(self, tmp_path) -> None:
        p = tmp_path / "config.toml"
        with mock.patch.object(ops, "CONFIG_PATH", str(p)):
            assert not ops.check_config_file().ok
        p.write_text("")
        with mock.patch.object(ops, "CONFIG_PATH", str(p)):
            assert ops.check_config_file().ok


class TestCatalog:
    def test_reads_runtime_catalog(self) -> None:
        c = ops.check_catalog()
        assert c.ok
        assert "2 model(s)" in c.detail

    def test_unreadable_fails(self) -> None:
        with mock.patch.object(ops, "_catalog_models", return_value=("", [])):
            c = ops.check_catalog()
        assert not c.ok
        assert c.fix


class TestDoctor:
    _CHECK_NAMES = ("check_api_key", "check_config_file", "check_config", "check_catalog",
                    "check_port", "check_service", "check_meter", "check_logs", "check_upstream")

    def _all_ok(self) -> list:
        return [mock.patch.object(ops, name, return_value=ops.Check(name, "ok", "fine")).start()
                for name in self._CHECK_NAMES]

    def test_json_all_ok(self) -> None:
        self._all_ok()
        try:
            assert ops.doctor(["--json"]) == 0
        finally:
            mock.patch.stopall()

    def test_json_failure(self) -> None:
        with mock.patch.object(ops, "check_api_key", return_value=ops.Check("api key", "fail", "missing", fix="fix it")), \
             mock.patch.object(ops, "check_service", return_value=ops.Check("service", "ok", "health 200")), \
             mock.patch.object(ops, "check_meter", return_value=ops.Check("meter", "ok", "w")), \
             mock.patch.object(ops, "check_logs", return_value=ops.Check("logs", "ok", "l")), \
             mock.patch.object(ops, "check_config", return_value=ops.Check("config", "ok", "c")), \
             mock.patch.object(ops, "check_config_file", return_value=ops.Check("config file", "ok", "cf")), \
             mock.patch.object(ops, "check_catalog", return_value=ops.Check("catalog", "ok", "cat")), \
             mock.patch.object(ops, "check_port", return_value=ops.Check("port", "ok", "p")), \
             mock.patch.object(ops, "check_upstream", return_value=ops.Check("upstream", "ok", "u")):
            rc = ops.doctor(["--json"])
        assert rc == 1

    def test_warn_does_not_fail(self) -> None:
        with mock.patch.object(ops, "check_api_key", return_value=ops.Check("api key", "ok", "k")), \
             mock.patch.object(ops, "check_service", return_value=ops.Check("service", "ok", "s")), \
             mock.patch.object(ops, "check_meter", return_value=ops.Check("meter", "ok", "m")), \
             mock.patch.object(ops, "check_logs", return_value=ops.Check("logs", "ok", "l")), \
             mock.patch.object(ops, "check_config", return_value=ops.Check("config", "ok", "c")), \
             mock.patch.object(ops, "check_config_file", return_value=ops.Check("config file", "ok", "cf")), \
             mock.patch.object(ops, "check_catalog", return_value=ops.Check("catalog", "ok", "cat")), \
             mock.patch.object(ops, "check_port", return_value=ops.Check("port", "ok", "p")), \
             mock.patch.object(ops, "check_upstream", return_value=ops.Check("upstream", "warn", "not reachable")):
            rc = ops.doctor(["--json"])
        assert rc == 0

    def test_fix_runs_safe_repairs(self) -> None:
        from opencode_go_proxy import catalog

        failing = [ops.Check("catalog", "fail", "unreadable", fix="doctor --fix")]
        with mock.patch.object(ops, "_run_checks", side_effect=[failing, []]), \
             mock.patch.object(catalog, "prepare_runtime_catalog") as prepare:
            ops.doctor(["--fix", "--json"])
        prepare.assert_called_once()

    def test_doctor_report_shape(self) -> None:
        report = ops._doctor_report([ops.Check("port", "warn", "occupied")])
        assert report["ok"] is True
        assert report["checks"][0]["status"] == "warn"


class TestSmoke:
    def test_ok(self) -> None:
        body = json.dumps({
            "model": "deepseek-v4-flash",
            "output_text": f"Sure: {ops.SMOKE_MARKER}",
            "output": [{"content": [{"text": ops.SMOKE_MARKER}]}],
        }).encode()
        with mock.patch("opencode_go_proxy.ops.urllib.request.urlopen", return_value=MockResp(body)):
            assert ops.smoke_test() == 0

    def test_http_error(self, capsys) -> None:
        with mock.patch("opencode_go_proxy.ops.urllib.request.urlopen", side_effect=_http_error(503, "proxy down")):
            assert ops.smoke_test() == 1
        out = capsys.readouterr().out
        assert "HTTP 503" in out
        assert "proxy down" in out

    def test_proxy_unreachable(self, capsys) -> None:
        with mock.patch("opencode_go_proxy.ops.urllib.request.urlopen",
                        side_effect=urllib.error.URLError("refused")):
            assert ops.smoke_test() == 1
        assert "not reachable" in capsys.readouterr().out

    def test_marker_missing(self, capsys) -> None:
        body = json.dumps({"output_text": "wrong answer"}).encode()
        with mock.patch("opencode_go_proxy.ops.urllib.request.urlopen", return_value=MockResp(body)):
            assert ops.smoke_test() == 1
        assert "missing marker" in capsys.readouterr().out

    def test_custom_base_env(self) -> None:
        body = json.dumps({"output_text": ops.SMOKE_MARKER}).encode()
        with mock.patch.dict(os.environ, {"OPENCODE_GO_PROXY_BASE_URL": "http://127.0.0.1:8790"}), \
             mock.patch("opencode_go_proxy.ops.urllib.request.urlopen", return_value=MockResp(body)) as urlopen:
            assert ops.smoke_test() == 0
        req = urlopen.call_args.args[0]
        assert req.full_url.startswith("http://127.0.0.1:8790/v1/responses")
        assert ("Content-type", "application/json") in req.header_items()

    def test_base_url_flag(self) -> None:
        body = json.dumps({"output_text": ops.SMOKE_MARKER}).encode()
        with mock.patch("opencode_go_proxy.ops.urllib.request.urlopen", return_value=MockResp(body)) as urlopen:
            assert ops.smoke_test(["--base-url", "http://127.0.0.1:9000/"]) == 0
        req = urlopen.call_args.args[0]
        assert req.full_url == "http://127.0.0.1:9000/v1/responses"


class TestMask:
    def test_redacts_secrets(self) -> None:
        assert ops._mask_secret_values('api_key = "sk-live"') == "api_key= ***redacted***"
        assert ops._mask_secret_values("api_key = 'sk-single'") == "api_key= ***redacted***"
        assert ops._mask_secret_values('OPENCODE_API_KEY = "sk-upper"') == "OPENCODE_API_KEY= ***redacted***"
        assert ops._mask_secret_values('env = { OPENCODE_API_KEY = "sk-inline" }') == "env = { OPENCODE_API_KEY= ***redacted*** }"
        assert ops._mask_secret_values('model = "deepseek-v4-flash"') == 'model = "deepseek-v4-flash"'

    def test_redacts_log_text(self) -> None:
        out = ops._redact_text("authorization: Bearer sk-live-secret-12345 reply sk-abcdefghijkl")
        assert "sk-live-secret-12345" not in out
        assert "sk-abcdefghijkl" not in out
        assert "[REDACTED]" in out

    def test_env_summary_redacts_secret_shaped(self) -> None:
        with mock.patch.dict(os.environ, {
            "OPENCODE_GO_API_KEY": "sk-topsecret",
            "OPENCODE_GO_PROXY_STATE_DIR": "/tmp/state",
            "CHAT_COMPLETIONS_BASE_URL": "https://opencode.ai/zen/go/v1",
        }, clear=True):
            summary = ops._env_summary()
        assert summary["OPENCODE_GO_API_KEY"] == "[REDACTED]"
        assert summary["OPENCODE_GO_PROXY_STATE_DIR"] == "/tmp/state"
        assert summary["CHAT_COMPLETIONS_BASE_URL"].startswith("https://")


class TestSupportBundle:
    def test_bundle_json_schema_and_redaction(self, tmp_path) -> None:
        cfg = tmp_path / "config.toml"
        cfg.write_text(
            'openai_base_url = "http://127.0.0.1:8787/v1"\n'
            'api_key = "sk-secret-value"\n'
            'OPENCODE_API_KEY = "sk-upper-style"\n'
        )
        log_dir = tmp_path / "logs"
        log_dir.mkdir()
        (log_dir / "opencode-go-proxy.err").write_text("2026-08-10 server.start sk-log-secret\n")
        state = tmp_path / "state"
        state.mkdir(exist_ok=True)
        (state / "usage-events.jsonl").write_text(
            '{"at":"2026-08-10T00:00:00Z","model":"deepseek-v4-flash","status":200}\n'
        )
        out = tmp_path / "bundle.json"
        with mock.patch.object(ops, "LOG_DIR", str(log_dir)), \
             mock.patch.object(ops, "CONFIG_PATH", str(cfg)), \
             mock.patch.dict(os.environ, {"OPENCODE_GO_PROXY_STATE_DIR": str(state)}):
            assert ops.support_bundle(["--output", str(out)]) == 0
        data = json.loads(out.read_text())
        assert data["schemaVersion"] == 1
        assert data["generatedAt"].endswith("Z")
        assert data["version"]
        assert data["runtime"]["python"]
        assert data["env"]["OPENCODE_GO_PROXY_STATE_DIR"] == str(state)
        assert "sk-secret-value" not in json.dumps(data)
        assert "sk-upper-style" not in json.dumps(data)
        assert "sk-log-secret" not in json.dumps(data)
        assert "***redacted***" in data["config"]["redacted"]
        assert data["config"]["exists"] is True
        assert data["meter"]["path"].endswith("usage-events.jsonl")
        assert data["meter"]["tail"][0]["model"] == "deepseek-v4-flash"
        assert data["logs"]["tail"]["opencode-go-proxy.err"]
        assert data["catalog"]["modelCount"] >= 2
        assert data["doctor"]["checks"]
        assert (out.stat().st_mode & 0o777) == 0o600

    def test_default_output_path(self, tmp_path, monkeypatch) -> None:
        monkeypatch.chdir(tmp_path)
        with mock.patch.object(ops, "_run_checks", return_value=[]):
            assert ops.support_bundle([]) == 0
        names = [n for n in os.listdir(tmp_path) if n.startswith("opencode-go-support-") and n.endswith(".json")]
        assert len(names) == 1


class TestInstall:
    def test_points_at_menu_bar_app(self, capsys) -> None:
        assert ops.install([]) == 0
        assert "menu bar" in capsys.readouterr().out


class TestInstallSkills:
    def test_dry_run_prints_target(self, tmp_path, capsys) -> None:
        with mock.patch.dict(os.environ, {"OPENCODE_GO_PROXY_SKILLS_DIR": str(tmp_path / "skills")}):
            assert ops.install_skills(["--dry-run"]) == 0
        out = capsys.readouterr().out
        assert out == str(tmp_path / "skills" / "opencode-go-proxy" / "SKILL.md") + "\n"
        assert not (tmp_path / "skills").exists()

    def test_installs_with_marker(self, tmp_path) -> None:
        source = tmp_path / "SKILL.md"
        source.write_text("---\nname: opencode-go-proxy\n---\n\nBody text.\n")
        target = tmp_path / "skills" / "opencode-go-proxy" / "SKILL.md"
        with mock.patch.dict(os.environ, {
            "OPENCODE_GO_PROXY_SKILLS_DIR": str(tmp_path / "skills"),
            "OPENCODE_GO_PROXY_SKILL_SOURCE": str(source),
        }):
            assert ops.install_skills([]) == 0
            assert ops.install_skills([]) == 0
        text = target.read_text()
        assert text.count(ops.SKILL_MARKER) == 1
        assert text.startswith("---\nname: opencode-go-proxy\n---\n")
        assert "Body text." in text

    def test_missing_source_fails(self, tmp_path, capsys) -> None:
        with mock.patch.dict(os.environ, {
            "OPENCODE_GO_PROXY_SKILLS_DIR": str(tmp_path / "skills"),
            "OPENCODE_GO_PROXY_SKILL_SOURCE": str(tmp_path / "missing.md"),
        }):
            assert ops.install_skills([]) == 1
        assert "not found" in capsys.readouterr().out


class TestStatus:
    def test_json_status(self, tmp_path, capsys) -> None:
        log_dir = tmp_path / "logs"
        log_dir.mkdir()
        (log_dir / "opencode-go-proxy.log").write_text("x\n")
        with mock.patch.object(ops, "LOG_DIR", str(log_dir)), \
             mock.patch.object(ops, "check_service", return_value=ops.Check("service", "ok", "health 200 on http://127.0.0.1:8787/health")), \
             mock.patch.object(ops, "check_port", return_value=ops.Check("port", "ok", "owned by proxy")):
            assert ops.status(["--json"]) == 0
        state = json.loads(capsys.readouterr().out)
        assert state["running"] is True
        assert state["port"] == 8787
        assert "launchd" not in state
        assert state["logs"]["files"] == ["opencode-go-proxy.log"]


class TestReviewFixups:
    def test_support_bundle_redacts_bearer_under_generic_key(self, tmp_path) -> None:
        # config values shaped like bearer credentials must not survive the bundle
        config = tmp_path / "config.toml"
        config.write_text('openai_base_url = "https://example.test/v1"\ncustom_key = "Bearer sk-abcdef1234567890"\n')
        with mock.patch("opencode_go_proxy.ops.CONFIG_PATH", str(config)):
            snapshot = ops._config_snapshot()
        assert "sk-abcdef1234567890" not in snapshot["redacted"]
        assert "REDACTED" in snapshot["redacted"]

    def test_check_config_ignores_comments_and_sections(self, tmp_path) -> None:
        config = tmp_path / "config.toml"
        config.write_text(
            '# openai_base_url = "http://127.0.0.1:8787"\n[model_providers.opencode-go]\nbase_url = "http://127.0.0.1:8787"\n'
        )
        with mock.patch("opencode_go_proxy.ops.CONFIG_PATH", str(config)):
            check = ops.check_config()
        assert check.ok is False
        config.write_text('openai_base_url = "http://127.0.0.1:8787/v1"\n')
        with mock.patch("opencode_go_proxy.ops.CONFIG_PATH", str(config)):
            check = ops.check_config()
        assert check.ok is True

    def test_check_port_detects_occupied_non_http_listener(self) -> None:
        import socket as sock_mod
        import threading

        listener = sock_mod.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]

        def answer() -> None:
            conn, _ = listener.accept()
            conn.close()

        threading.Thread(target=answer, daemon=True).start()
        try:
            check = ops.check_port(f"http://127.0.0.1:{port}/health")
            assert check.ok is False
            assert "occupied" in check.detail
        finally:
            listener.close()


class TestReviewFixups2:
    def test_pem_private_key_fully_redacted(self, tmp_path) -> None:
        pem = (
            "-----BEGIN RSA PRIVATE KEY-----\nMIIEpQIBAAKCAQEA\nfakeline\n"
            "-----END RSA PRIVATE KEY-----\n"
        )
        config = tmp_path / "config.toml"
        config.write_text(f'custom = "{pem}"\n')
        with mock.patch("opencode_go_proxy.ops.CONFIG_PATH", str(config)):
            snapshot = ops._config_snapshot()
        assert "PRIVATE KEY" not in snapshot["redacted"]
        assert "REDACTED" in snapshot["redacted"]

    def test_check_config_single_quoted_root(self, tmp_path) -> None:
        config = tmp_path / "config.toml"
        config.write_text("openai_base_url = 'http://127.0.0.1:8787/v1'\n")
        with mock.patch("opencode_go_proxy.ops.CONFIG_PATH", str(config)):
            assert ops.check_config().ok is True

    def test_check_config_ignores_table_section_value(self, tmp_path) -> None:
        config = tmp_path / "config.toml"
        config.write_text('[model_providers.opencode-go]\nopenai_base_url = "http://127.0.0.1:8787/v1"\n')
        with mock.patch("opencode_go_proxy.ops.CONFIG_PATH", str(config)):
            assert ops.check_config().ok is False

    def test_check_config_requires_exact_port(self, tmp_path) -> None:
        config = tmp_path / "config.toml"
        config.write_text('openai_base_url = "http://127.0.0.1:9999/v1"\n')
        with mock.patch("opencode_go_proxy.ops.CONFIG_PATH", str(config)):
            assert ops.check_config().ok is False
