"""Structured stderr tracing shared by the proxy modules.

Every module writes one JSON object per line to stderr; the support bundle
collects these as the operational log. Tracing never prints secrets: error
bodies are masked before they leave this module.
"""

from __future__ import annotations

import json
import re
import sys
import time
from typing import Any

# Secret-looking content masked out of upstream error bodies before they are
# traced to stderr (and later shipped in a support bundle). An upstream that
# echoes the request Authorization header back in its error body would
# otherwise leak the API key into logs verbatim.
_MASK_RE = re.compile(
    r"(?i)(authorization[\s\"']*[:=][\s\"']*)[^\"'\r\n,;]+|(\bsk-[A-Za-z0-9_\-]{8,}\b)"
)


def _mask_trace_body(body: str, limit: int = 2000) -> str:
    text = body[:limit]

    def _repl(m: re.Match) -> str:
        if m.group(1):
            return f"{m.group(1)}<redacted>"
        return "<redacted>"

    return _MASK_RE.sub(_repl, text)


def trace(event: str, **fields: Any) -> None:
    record = {"ts": time.time(), "event": event, **fields}
    print(json.dumps(record, sort_keys=True), file=sys.stderr, flush=True)
