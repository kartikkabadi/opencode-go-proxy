"""Vision caption helpers: cache, engine pick, and cheap image input.

Plan 001's urgent latency subset lives here so the fuller vision bridge
(wayfinder ticket 14) can extend the same cache/engine structure instead of
rewriting it. Stdlib only; ``sips`` (macOS system tool) is used for the
downscale fallback.
"""

from __future__ import annotations

import base64
import hashlib
import os
import shutil
import subprocess
import tempfile
import threading
import time
from typing import Any

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


def resolve_caption_model(target_model: str) -> str:
    """Pick the caption engine: explicit override, else the turn model.

    ``CODEX_IMAGE_MODEL`` keeps its role as the hard override. The new
    ``OPENCODE_GO_PROXY_CAPTION_MODEL`` default ``auto`` makes the turn model
    the caption engine; setting it to a concrete model pins the engine. A 4xx
    from the upstream still falls back to ``IMAGE_MODEL_DEFAULT`` in app.py.
    """
    explicit = os.environ.get("CODEX_IMAGE_MODEL")
    if explicit:
        return explicit
    auto = os.environ.get("OPENCODE_GO_PROXY_CAPTION_MODEL", "auto")
    if auto.strip().lower() not in {"", "auto"}:
        return auto.strip()
    return target_model or IMAGE_MODEL_DEFAULT


def caption_detail() -> str | None:
    """Detail level for the caption image part; ``low`` default, ``none`` disables."""
    value = os.environ.get("OPENCODE_GO_PROXY_CAPTION_DETAIL", "low").strip().lower()
    if value in {"", "none", "off", "false"}:
        return None
    if value in {"high", "full"}:
        return "high"
    return "low"


def build_caption_payload(image_url: str, model: str, *, detail: str | None = "low") -> dict[str, Any]:
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
                    {"type": "text", "text": CAPTION_PROMPT},
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


def _caption_rejection_status(exc: Any) -> int:
    return exc.upstream_status if exc.upstream_status is not None else int(exc.status)


def _caption_attempt(image_url: str, image_model: str, detail: str | None, config: Any, request_id: str) -> tuple[dict[str, Any] | None, Any | None]:
    """One caption sub-call with no transient retries; returns (chat, error)."""
    from .upstream import call_upstream_chat, caption_timeout_sec

    payload = build_caption_payload(image_url, image_model, detail=detail)
    try:
        chat, _retries = call_upstream_chat(payload, config, request_id, timeout_sec=caption_timeout_sec(), max_retries=0)
        return chat, None
    except Exception as exc:  # noqa: BLE001 - surfaced as a failed caption
        return None, exc


def caption_image(image_url: str, image_model: str, config: Any, request_id: str) -> str:
    """Caption one image via a vision-capable model, served from a byte-keyed cache.

    Returns a text description. A failed caption degrades to a placeholder;
    it never blocks the turn.
    """
    from .trace import trace
    from .upstream import record_cache

    image_bytes = image_bytes_for_cache(image_url)
    cached = CAPTION_CACHE.get(image_bytes)
    if cached is not None:
        trace("split_turn.caption_cache_hit", request_id=request_id)
        return cached

    detail = caption_detail()
    url, model = image_url, image_model
    chat, exc = _caption_attempt(url, model, detail, config, request_id)
    if exc is not None and _caption_rejection_status(exc) in CAPTION_FALLBACK_STATUSES and model != IMAGE_MODEL_DEFAULT:
        # The turn model may reject image input; fall back to the known
        # vision model for this one sub-call.
        model = IMAGE_MODEL_DEFAULT
        trace("split_turn.caption_fallback", request_id=request_id, kind="engine", model=model, status=_caption_rejection_status(exc))
        chat, exc = _caption_attempt(url, model, detail, config, request_id)
    if exc is not None and _caption_rejection_status(exc) in CAPTION_FALLBACK_STATUSES and detail is not None:
        # Unknown detail values can 4xx on some upstreams; retry with a
        # downscaled image (or the original URL) and no detail.
        downscaled = downscale_data_url(url)
        if downscaled is not None:
            url = downscaled
        detail = None
        trace("split_turn.caption_fallback", request_id=request_id, kind="detail", status=_caption_rejection_status(exc))
        chat, exc = _caption_attempt(url, model, detail, config, request_id)
    if exc is not None:
        trace("split_turn.caption_failed", request_id=request_id, status=getattr(exc, "status", None), message=str(getattr(exc, "message", exc))[:200])
        return f"[caption failed: {str(getattr(exc, 'message', exc))[:100]}]"

    record_cache(config.cache_tracker, model, chat.get("usage"))
    choice = (chat.get("choices") or [{}])[0]
    text = (choice.get("message", {}) or {}).get("content", "")
    caption = text.strip() if isinstance(text, str) and text.strip() else "[caption unavailable]"
    CAPTION_CACHE.put(image_bytes, caption)
    return caption


def caption_images_in_messages(chat_payload: dict[str, Any], target_model: str, config: Any, request_id: str) -> dict[str, Any]:
    """Replace image_url parts with vision-generated text captions. Routes turn to target_model after."""
    from .trace import trace

    image_model = resolve_caption_model(target_model)
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
    caption = caption_image(latest[2], image_model, config, request_id)
    for mi, pi, _url in image_jobs[:-1]:
        messages[mi]["content"][pi] = {"type": "text", "text": "[prior screenshot omitted]"}
    mi, pi, _ = latest
    messages[mi]["content"][pi] = {"type": "text", "text": f"[screenshot: {caption}]"}

    # Collapse text-only lists back to strings (fast path for upstream).
    for message in messages:
        content = message.get("content")
        if isinstance(content, list) and all(
            isinstance(p, dict) and p.get("type") == "text" for p in content
        ):
            message["content"] = "\n".join(p.get("text", "") for p in content if p.get("text"))

    chat_payload["model"] = target_model
    trace("split_turn.captioned", request_id=request_id, captions=1, omitted=len(image_jobs) - 1, model=chat_payload["model"])
    return chat_payload
