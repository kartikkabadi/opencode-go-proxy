"""Vision bridge: structured evidence read from image-capable engines.

Plan 004 turns the caption path (plan 001's latency subset) into a real
module. :func:`describe` reads one image through a vision engine and returns
structured :class:`Evidence` (summary / text / layout / unreadable) so a
downstream model gets something to quote instead of a vague impression.
Engines sit behind one adapter interface: remote chat completions through
upstream.py, and local runtimes (Ollama, llama.cpp server, LM Studio) that are
probed read-only before they are ever nominated. Stdlib only; ``sips``
(macOS system tool) is used for the downscale fallback.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from http import HTTPStatus
from typing import Any

from .errors import ProxyError
from .meter import record_usage_event
from .protocol import IMAGE_MODEL_DEFAULT

# Caption cache: the same screenshot bytes are re-sent on every tool call of a
# turn, and each re-caption costs a full upstream round trip. Key by the raw
# image bytes, hold for an hour, cap at 256 entries and evict the oldest.
CAPTION_CACHE_TTL_SEC = 3600.0
CAPTION_CACHE_MAX_ENTRIES = 256

# Longest edge cap for the sips downscale fallback (retina screenshots are
# far larger than any vision model needs).
SIPS_MAX_EDGE = 1600

CAPTION_PROMPT = (
    "You are captioning a screenshot for a coding agent that cannot see images. "
    "The agent needs to click elements precisely, so spatial positions are critical. "
    "Describe in 4-6 sentences: (1) app name and what window/panel is active, "
    "(2) list every clickable element with its approximate position as (x,y) pixels "
    "from top-left - buttons, menu items, links, input fields, toolbar icons. "
    "Format: 'button \"Save\" at (120, 45)', 'input field at (300, 200)', etc. "
    "(3) any visible text content - quote exactly. "
    "(4) where the cursor/focus/selection currently is. "
    "Skip colors and styling unless they convey state (e.g. red error, green success)."
)

# Evidence kinds the contract returns. layout preserves spatial guidance so a
# text-only model can still target clicks; text marks a verbatim transcript;
# summary is a plain description; unreadable is an empty or failed read.
EVIDENCE_KINDS = ("summary", "text", "layout", "unreadable")


class CaptionCache:
    """In-process caption cache keyed by image bytes, TTL- and size-bounded."""

    def __init__(self, *, ttl_sec: float = CAPTION_CACHE_TTL_SEC, max_entries: int = CAPTION_CACHE_MAX_ENTRIES) -> None:
        self.ttl_sec = ttl_sec
        self.max_entries = max_entries
        self._entries: dict[str, tuple[float, str]] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _key(image_bytes: bytes) -> str:
        return hashlib.sha256(image_bytes).hexdigest()

    def get(self, image_bytes: bytes) -> str | None:
        key = self._key(image_bytes)
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            stamp, caption = entry
            if time.time() - stamp > self.ttl_sec:
                del self._entries[key]
                return None
            return caption

    def put(self, image_bytes: bytes, caption: str) -> None:
        key = self._key(image_bytes)
        with self._lock:
            self._entries[key] = (time.time(), caption)
            if len(self._entries) <= self.max_entries:
                return
            oldest_key = min(self._entries, key=lambda k: self._entries[k][0])
            del self._entries[oldest_key]

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)


CAPTION_CACHE = CaptionCache()


def image_bytes_for_cache(image_url: str) -> bytes:
    """Bytes to key the cache on: decoded data-URL payload, else the URL itself.

    Codex screenshots arrive as data URLs, so the payload bytes are the stable
    key. Remote URLs are keyed by the URL string (stable within a turn) rather
    than fetched, so a cache hit never costs an extra network round trip.
    """
    if image_url.startswith("data:"):
        try:
            _header, payload = image_url.split(",", 1)
            return base64.b64decode(payload, validate=True)
        except (ValueError, base64.binascii.Error):
            return image_url.encode("utf-8", errors="replace")
    return image_url.encode("utf-8", errors="replace")


def caption_detail() -> str | None:
    """Detail level for the caption image part; ``low`` default, ``none`` disables."""
    value = os.environ.get("OPENCODE_GO_PROXY_CAPTION_DETAIL", "low").strip().lower()
    if value in {"", "none", "off", "false"}:
        return None
    if value in {"high", "full"}:
        return "high"
    return "low"


def build_caption_payload(image_url: str, model: str, *, detail: str | None = "low", prompt: str = CAPTION_PROMPT) -> dict[str, Any]:
    """Chat payload for one caption sub-call, image detail included."""
    image: dict[str, Any] = {"url": image_url}
    if detail:
        image["detail"] = detail
    return {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": image},
                ],
            }
        ],
        "stream": False,
        "max_tokens": 200,
    }


def downscale_data_url(image_url: str, max_edge: int = SIPS_MAX_EDGE) -> str | None:
    """Downscale a data-URL image with macOS ``sips`` and re-embed as JPEG.

    Returns None when the image is not a data URL, ``sips`` is unavailable, or
    the conversion fails. Only ever applied to the caption sub-call, never to
    the main payload.
    """
    if not image_url.startswith("data:"):
        return None
    sips = shutil.which("sips")
    if not sips:
        return None
    try:
        _header, payload = image_url.split(",", 1)
        raw = base64.b64decode(payload, validate=True)
    except (ValueError, base64.binascii.Error):
        return None
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            src = os.path.join(tmpdir, "caption-src.png")
            dst = os.path.join(tmpdir, "caption-dst.jpg")
            with open(src, "wb") as fh:
                fh.write(raw)
            subprocess.run(
                [sips, "-Z", str(max_edge), "-s", "format", "jpeg", "-s", "formatOptions", "85", src, "--out", dst],
                check=True,
                capture_output=True,
                timeout=20,
            )
            with open(dst, "rb") as fh:
                out = fh.read()
    except (OSError, subprocess.SubprocessError):
        return None
    if not out:
        return None
    return "data:image/jpeg;base64," + base64.b64encode(out).decode("ascii")


# Only these 4xx statuses mean "this model cannot read the image" or "this
# detail value is unsupported". Auth failures (401/403) and rate limits (429)
# degrade immediately so a bad key never fans out into three caption calls.
CAPTION_FALLBACK_STATUSES = {400, 404, 415, 422}


def is_image_rejection_status(status: int | None) -> bool:
    """Whether an HTTP status means "this model cannot read the image".

    The same 4xx set the caption path already falls back on (plan 001/004)
    rescues a main image turn: a catalog-promised model that rejects image
    input at runtime gets one caption-and-retry instead of a failed turn.
    """
    return status in CAPTION_FALLBACK_STATUSES


@dataclass(frozen=True)
class Evidence:
    """One structured reading of an image for a model that cannot see it."""

    kind: str
    text: str
    model: str | None = None
    cached: bool = False


@dataclass(frozen=True)
class AdapterResult:
    """Adapter outcome: classified evidence text plus the engine that read it."""

    kind: str
    text: str
    model: str | None
    usage: Any = None


_COORD_PAIR_RE = re.compile(r"\(\s*-?\d{1,5}\s*[,;]\s*-?\d{1,5}\s*\)")
_FAILED_PREFIXES = ("[caption failed", "[caption unavailable")


def classify_evidence(text: str) -> str:
    """Map engine output to an evidence kind.

    ``layout`` keeps the spatial guidance a text-only agent needs for click
    precision; ``text`` marks a verbatim-transcript shaped answer; ``summary``
    is a plain description; ``unreadable`` is an empty or failed read.
    """
    stripped = (text or "").strip()
    if not stripped or stripped.startswith(_FAILED_PREFIXES):
        return "unreadable"
    if _COORD_PAIR_RE.search(stripped):
        return "layout"
    if re.search(r"(?im)^#{1,6}\s+(?:text|layout|data|uncertain)\b", stripped):
        return "text"
    return "summary"


def _caption_rejection_status(exc: Any) -> int:
    return exc.upstream_status if exc.upstream_status is not None else int(exc.status)


def _error_status(exc: Exception) -> int:
    status = getattr(exc, "status", None)
    return int(status) if status is not None else int(HTTPStatus.BAD_GATEWAY)


def _normalize_engine_name(value: str) -> str:
    """Drop the ``opencode-go/`` prefix the reference router uses in config."""
    name = value.strip()
    if name.startswith("opencode-go/"):
        return name[len("opencode-go/") :]
    return name


def _caption_engine_setting() -> str:
    """Env-chosen engine: a model slug or ``local``; ``""`` means auto.

    ``CODEX_IMAGE_MODEL`` stays the hard override. ``OPENCODE_GO_PROXY_CAPTION_MODEL``
    pins a concrete model, ``local``, or falls back to auto.
    """
    explicit = os.environ.get("CODEX_IMAGE_MODEL")
    if explicit and explicit.strip():
        return _normalize_engine_name(explicit)
    value = os.environ.get("OPENCODE_GO_PROXY_CAPTION_MODEL", "auto")
    if value.strip().lower() in {"", "auto"}:
        return ""
    return _normalize_engine_name(value)


def supports_image_input(model: dict[str, Any]) -> bool:
    """True when the catalog record declares image input."""
    return "image" in (model.get("input_modalities") or [])


def catalog_image_models() -> list[dict[str, Any]]:
    """Image-capable, listed models from the full-shape state catalog."""
    from . import catalog as _catalog

    path = _catalog.default_catalog_path()
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return []
    models = data.get("models") if isinstance(data, dict) else None
    if not isinstance(models, list):
        return []
    return [
        m
        for m in models
        if isinstance(m, dict) and supports_image_input(m) and m.get("visibility", "list") == "list"
    ]


# The catalog carries no per-token prices, so name-tier hints rank engines by
# relative cost, mirroring the reference router: flash/haiku/mini/lite/small/
# turbo are the cheap tiers, then priority, then slug as a stable tiebreak.
_CHEAP_ENGINE_HINTS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (r"flash", r"haiku", r"mini(?!max)", r"lite", r"small", r"turbo")
)


def _cost_rank(slug: str) -> int:
    for index, hint in enumerate(_CHEAP_ENGINE_HINTS):
        if hint.search(slug):
            return index
    return len(_CHEAP_ENGINE_HINTS)


def rank_catalog_image_models(models: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Cheapest-first order: cost hint, then priority, then slug."""
    return sorted(
        models,
        key=lambda m: (_cost_rank(str(m.get("slug", ""))), m.get("priority", 999), str(m.get("slug", ""))),
    )


def auto_pick_caption_model() -> str:
    """Cheapest image-capable catalog model, or ``IMAGE_MODEL_DEFAULT``."""
    ranked = rank_catalog_image_models(catalog_image_models())
    if ranked:
        slug = str(ranked[0].get("slug", ""))
        if slug:
            return slug
    return IMAGE_MODEL_DEFAULT


def resolve_caption_model(target_model: str) -> str:
    """Resolve the caption engine to a model slug (or ``local``), env first.

    The turn model no longer drives auto: the cheapest image-capable catalog
    model does, with ``mimo-v2.5`` as the fallback when the catalog has none.
    ``target_model`` is accepted for compatibility; auto ignores it.
    """
    setting = _caption_engine_setting()
    if setting:
        return setting
    return auto_pick_caption_model()


# Local runtime (Ollama / llama.cpp server / LM Studio) config. All three
# expose the OpenAI-compatible /v1 surface, so one adapter covers them.
LOCAL_VISION_BASE_URL_ENV = "OPENCODE_GO_PROXY_VISION_LOCAL_BASE_URL"
LOCAL_VISION_MODEL_ENV = "OPENCODE_GO_PROXY_VISION_LOCAL_MODEL"
DEFAULT_LOCAL_VISION_BASE_URL = "http://127.0.0.1:11434/v1"
DEFAULT_LOCAL_VISION_MODEL = "qwen2.5vl:3b"
LOCAL_PROBE_TIMEOUT_SEC = 1.5
_LOCAL_PROBE_TTL_SEC = 30.0

# Vision model names a local runtime must expose before auto will nominate it;
# a runtime that is up but only serves text models is "not enabled".
_LOCAL_VISION_KEYWORDS = re.compile(
    r"(qwen2\.?5?-?vl|qwen2-vl|llama3\.2-vision|llava|moondream|gemma3|"
    r"minicpm-v|internvl|phi-4-vision|phi-3-vision|bakllava|cogvlm|glm-4v)",
    re.IGNORECASE,
)

_LOCAL_PROBE_CACHE: dict[tuple[str, str], tuple[float, bool]] = {}


def local_vision_base_url() -> str:
    return os.environ.get(LOCAL_VISION_BASE_URL_ENV, DEFAULT_LOCAL_VISION_BASE_URL).strip() or DEFAULT_LOCAL_VISION_BASE_URL


def local_vision_model() -> str:
    return os.environ.get(LOCAL_VISION_MODEL_ENV, DEFAULT_LOCAL_VISION_MODEL).strip() or DEFAULT_LOCAL_VISION_MODEL


def _probe_local(base_url: str, model: str) -> bool:
    """Read-only probe: does this runtime exist and serve the configured model?

    The configured model must be listed; a runtime that is up but only serves
    other models is "not enabled", so captions never fire against a model the
    runtime does not have.
    """
    url = f"{base_url}/models"
    try:
        with urllib.request.urlopen(url, timeout=LOCAL_PROBE_TIMEOUT_SEC) as response:
            if response.status != 200:
                return False
            payload = json.load(response)
    except Exception:  # noqa: BLE001 - a probe that can raise would break turns
        # A missing runtime (refused connection, timeout) or malformed reply
        # simply means "not enabled"; the probe never raises into the turn.
        return False
    entries = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(entries, list):
        return False
    ids = [str(entry.get("id", "")) for entry in entries if isinstance(entry, dict)]
    return model in ids


def local_runtime_enabled(*, base_url: str | None = None, model: str | None = None) -> bool:
    """Whether the local runtime is up and vision-capable, cached briefly.

    A negative result is cached too, so a stopped runtime costs one short
    localhost probe per window instead of one per image turn.
    """
    base_url = (base_url or local_vision_base_url()).rstrip("/")
    model = model or local_vision_model()
    key = (base_url, model)
    now = time.time()
    cached = _LOCAL_PROBE_CACHE.get(key)
    if cached is not None and now - cached[0] < _LOCAL_PROBE_TTL_SEC:
        return cached[1]
    enabled = _probe_local(base_url, model)
    _LOCAL_PROBE_CACHE[key] = (now, enabled)
    return enabled


def clear_local_probe_cache() -> None:
    """Forget cached probe results (tests switch runtimes per case)."""
    _LOCAL_PROBE_CACHE.clear()


def _caption_attempt(
    image_url: str, image_model: str, detail: str | None, prompt: str, config: Any, request_id: str
) -> tuple[dict[str, Any] | None, Any | None]:
    """One caption sub-call with no transient retries; returns (chat, error)."""
    from .upstream import call_upstream_chat, caption_timeout_sec

    payload = build_caption_payload(image_url, image_model, detail=detail, prompt=prompt)
    try:
        chat, _retries = call_upstream_chat(payload, config, request_id, timeout_sec=caption_timeout_sec(), max_retries=0)
        return chat, None
    except Exception as exc:  # noqa: BLE001 - surfaced as a failed caption
        return None, exc


def _caption_text_from_chat(chat: dict[str, Any]) -> str:
    choice = (chat.get("choices") or [{}])[0]
    text = (choice.get("message", {}) or {}).get("content", "")
    return text.strip() if isinstance(text, str) and text.strip() else "[caption unavailable]"


class RemoteVisionAdapter:
    """Vision engine reached through the proxy's upstream chat-completions client."""

    def __init__(self, model: str) -> None:
        self.model = model

    def describe(
        self, image_url: str, prompt: str, config: Any, request_id: str
    ) -> tuple[AdapterResult | None, Exception | None]:
        from .trace import trace

        detail = caption_detail()
        url, model = image_url, self.model
        chat, exc = _caption_attempt(url, model, detail, prompt, config, request_id)
        if exc is not None and _caption_rejection_status(exc) in CAPTION_FALLBACK_STATUSES and model != IMAGE_MODEL_DEFAULT:
            # The pinned/auto model may reject image input; fall back to the
            # known vision model for this one sub-call.
            model = IMAGE_MODEL_DEFAULT
            trace("split_turn.caption_fallback", request_id=request_id, kind="engine", model=model, status=_caption_rejection_status(exc))
            chat, exc = _caption_attempt(url, model, detail, prompt, config, request_id)
        if exc is not None and _caption_rejection_status(exc) in CAPTION_FALLBACK_STATUSES and detail is not None:
            # Unknown detail values can 4xx on some upstreams; retry with a
            # downscaled image (or the original URL) and no detail.
            downscaled = downscale_data_url(url)
            if downscaled is not None:
                url = downscaled
            detail = None
            trace("split_turn.caption_fallback", request_id=request_id, kind="detail", status=_caption_rejection_status(exc))
            chat, exc = _caption_attempt(url, model, detail, prompt, config, request_id)
        if exc is not None:
            return None, exc
        if config is not None:
            from .upstream import record_cache

            # Keep the prefix-cache tracker fed: caption reads count toward the
            # provider's cache accounting just like main-turn reads did.
            record_cache(config.cache_tracker, model, chat.get("usage"))
        text = _caption_text_from_chat(chat)
        return AdapterResult(kind=classify_evidence(text), text=text, model=model, usage=chat.get("usage")), None


def _local_engine_error(exc: Exception, model: str) -> ProxyError:
    if isinstance(exc, urllib.error.HTTPError):
        return ProxyError(HTTPStatus.BAD_GATEWAY, f"local vision engine {model} HTTP {exc.code}", upstream_status=exc.code)
    return ProxyError(HTTPStatus.BAD_GATEWAY, f"local vision engine {model}: {exc}")


class LocalVisionAdapter:
    """Vision engine on this machine (Ollama, llama.cpp server, LM Studio).

    All three speak the OpenAI-compatible chat-completions shape, so one
    request path covers them. No credential; one bounded attempt, no retries.
    """

    def __init__(self, *, base_url: str, model: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model

    def describe(
        self, image_url: str, prompt: str, config: Any, request_id: str
    ) -> tuple[AdapterResult | None, Exception | None]:
        from .upstream import caption_timeout_sec

        payload = build_caption_payload(image_url, self.model, detail=None, prompt=prompt)
        raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        url = f"{self.base_url}/chat/completions"
        request = urllib.request.Request(
            url, data=raw, headers={"content-type": "application/json"}, method="POST"
        )
        try:
            with urllib.request.urlopen(request, timeout=caption_timeout_sec()) as response:
                body = response.read()
        except Exception as exc:  # noqa: BLE001 - surfaced as a failed caption
            return None, _local_engine_error(exc, self.model)
        try:
            value = json.loads(body)
        except json.JSONDecodeError:
            return None, _local_engine_error(ValueError("invalid JSON from local vision engine"), self.model)
        if not isinstance(value, dict):
            return None, _local_engine_error(ValueError("non-object response from local vision engine"), self.model)
        text = _caption_text_from_chat(value)
        return AdapterResult(kind=classify_evidence(text), text=text, model=self.model), None


VisionAdapter = RemoteVisionAdapter | LocalVisionAdapter


def resolve_engines(target_model: str) -> list[VisionAdapter]:
    """Ordered caption engines for this turn: env pins first, then auto.

    Auto prefers an enabled local runtime (free, operator-owned) over the
    cheapest image-capable catalog model; ``mimo-v2.5`` is the catalog
    fallback. ``target_model`` is accepted for compatibility; auto ignores it.
    """
    setting = _caption_engine_setting()
    if setting == "local":
        return [LocalVisionAdapter(base_url=local_vision_base_url(), model=local_vision_model())]
    if setting:
        return [RemoteVisionAdapter(setting)]
    engines: list[VisionAdapter] = []
    if local_runtime_enabled():
        engines.append(LocalVisionAdapter(base_url=local_vision_base_url(), model=local_vision_model()))
    engines.append(RemoteVisionAdapter(auto_pick_caption_model()))
    return engines


def describe(
    image_url: str,
    prompt: str = CAPTION_PROMPT,
    *,
    config: Any = None,
    request_id: str = "",
    engines: list[VisionAdapter] | None = None,
    target_model: str | None = None,
) -> Evidence:
    """Read one image into structured :class:`Evidence`, served from cache.

    A cache hit returns instantly and is never metered. A miss walks the
    engines in order (auto: local then remote) and meters every non-cached
    read with ``kind=vision``; total failure degrades to unreadable evidence
    that never blocks the turn.
    """
    from .trace import trace
    from .upstream import usage_tokens

    image_bytes = image_bytes_for_cache(image_url)
    cached = CAPTION_CACHE.get(image_bytes)
    if cached is not None:
        trace("split_turn.caption_cache_hit", request_id=request_id)
        return Evidence(kind=classify_evidence(cached), text=cached, cached=True)

    if engines is None:
        engines = resolve_engines(target_model or "")

    last_adapter: VisionAdapter | None = None
    last_error: Exception | None = None
    started_all = time.time()
    for adapter in engines:
        result, exc = adapter.describe(image_url, prompt, config, request_id)
        if exc is None:
            model = result.model or adapter.model
            inp, outp, total = usage_tokens(result.usage)
            record_usage_event(
                model=model,
                status=200,
                duration_ms=int((time.time() - started_all) * 1000),
                input_tokens=inp,
                output_tokens=outp,
                total_tokens=total,
                kind="vision",
            )
            CAPTION_CACHE.put(image_bytes, result.text)
            return Evidence(kind=result.kind, text=result.text, model=model, cached=False)
        last_adapter = adapter
        last_error = exc
        trace(
            "split_turn.caption_failed",
            request_id=request_id,
            engine=adapter.model,
            status=_error_status(exc),
            message=str(getattr(exc, "message", exc))[:200],
        )

    if last_error is None:
        last_error = ProxyError(HTTPStatus.BAD_GATEWAY, "no vision engine available")
    record_usage_event(
        model=last_adapter.model if last_adapter else None,
        status=_error_status(last_error),
        duration_ms=int((time.time() - started_all) * 1000),
        kind="vision",
    )
    text = f"[caption failed: {str(getattr(last_error, 'message', last_error))[:100]}]"
    return Evidence(kind="unreadable", text=text, model=last_adapter.model if last_adapter else None, cached=False)


def caption_images_in_messages(chat_payload: dict[str, Any], target_model: str, config: Any, request_id: str) -> dict[str, Any]:
    """Replace image_url parts with vision-generated text captions. Routes turn to target_model after."""
    from .trace import trace

    engines = resolve_engines(target_model)
    messages = chat_payload.get("messages", [])

    # Collect all image URLs across messages.
    image_jobs: list[tuple[int, int, str]] = []  # (msg_idx, part_idx, url)
    for mi, message in enumerate(messages):
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for pi, part in enumerate(content):
            if isinstance(part, dict) and part.get("type") == "image_url":
                url = (part.get("image_url") or {}).get("url", "")
                if url:
                    image_jobs.append((mi, pi, url))

    if not image_jobs:
        chat_payload["model"] = target_model
        return chat_payload

    # Only caption the latest image; stub older ones to save 25+ seconds per turn.
    # Old screenshots are stale context — the model only needs the current screen to act.
    latest = image_jobs[-1]
    evidence = describe(latest[2], CAPTION_PROMPT, config=config, request_id=request_id, engines=engines)
    for mi, pi, _url in image_jobs[:-1]:
        messages[mi]["content"][pi] = {"type": "text", "text": "[prior screenshot omitted]"}
    mi, pi, _ = latest
    messages[mi]["content"][pi] = {"type": "text", "text": f"[screenshot: {evidence.text}]"}

    # Collapse text-only lists back to strings (fast path for upstream).
    for message in messages:
        content = message.get("content")
        if isinstance(content, list) and all(
            isinstance(p, dict) and p.get("type") == "text" for p in content
        ):
            message["content"] = "\n".join(p.get("text", "") for p in content if p.get("text"))

    chat_payload["model"] = target_model
    trace(
        "split_turn.captioned",
        request_id=request_id,
        captions=1,
        omitted=len(image_jobs) - 1,
        engine=evidence.model or "auto",
        kind=evidence.kind,
        cached=evidence.cached,
        model=chat_payload["model"],
    )
    return chat_payload
