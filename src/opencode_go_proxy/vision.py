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
