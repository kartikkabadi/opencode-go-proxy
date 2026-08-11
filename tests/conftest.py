"""Isolate all tests from the real proxy state dir (~/.codex/opencode-go-proxy/).

The HTTP handlers record usage-meter events on every turn. Without this
fixture, integration tests write fake events into the live state dir,
polluting the real meter. Autouse so no test can forget.

The fixture also seeds a minimal full-shape catalog in the isolated state dir
so routing and /v1/models tests never depend on the real machine's catalog
file (~/.codex/model-catalogs/opencode-go.json).
"""
import json
import os

import pytest


@pytest.fixture(autouse=True)
def isolated_state_dir(tmp_path) -> None:
    old = os.environ.get("OPENCODE_GO_PROXY_STATE_DIR")
    state = tmp_path / "state"
    os.environ["OPENCODE_GO_PROXY_STATE_DIR"] = str(state)
    state.mkdir(exist_ok=True)
    (state / "opencode-go-catalog.json").write_text(
        json.dumps(
            {
                "fetched_at": "2026-08-10T00:00:00Z",
                "etag": 'W/"test-fixture"',
                "client_version": "0.147.0",
                "models": [
                    {
                        "slug": "deepseek-v4-flash",
                        "display_name": "DeepSeek V4 Flash",
                        "description": "Test fixture.",
                        "input_modalities": ["text", "image"],
                    },
                    {
                        "slug": "deepseek-v4-pro",
                        "display_name": "DeepSeek V4 Pro",
                        "description": "Test fixture.",
                        "input_modalities": ["text", "image"],
                    },
                ],
            }
        )
    )
    yield
    if old is None:
        os.environ.pop("OPENCODE_GO_PROXY_STATE_DIR", None)
    else:
        os.environ["OPENCODE_GO_PROXY_STATE_DIR"] = old


@pytest.fixture(autouse=True)
def isolated_estimate_state() -> None:
    """Zero-input estimation latches are process-global; clear them per test.

    A model that reported real tokens in one test would otherwise disable
    estimation for every later test using the same slug.
    """
    from opencode_go_proxy import meter

    meter.clear_estimate_latches()
    yield
    meter.clear_estimate_latches()


@pytest.fixture(autouse=True)
def isolated_caption_cache() -> None:
    """Caption cache and local-probe state are process-global; clear them.

    A shared caption hit would make integration tests pass without exercising
    the upstream, and a cached local-runtime probe would leak one test's
    runtime state into the next.
    """
    from opencode_go_proxy import vision

    vision.CAPTION_CACHE.clear()
    vision.clear_local_probe_cache()
    yield
    vision.CAPTION_CACHE.clear()
    vision.clear_local_probe_cache()
