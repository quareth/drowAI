"""Universal chunking heuristics for artifact logs.

Implements lightweight boundary detection and chunking targeting ~600–800 tokens
per chunk. Uses tiktoken via token_utils for token counts when available.
"""

from __future__ import annotations

import hashlib
import re
from typing import Dict, List, Tuple

from agent.context.token_utils import count_tokens


TIMESTAMP_RE = re.compile(r"^\s*(?:\d{4}-\d{2}-\d{2}|\d{2}:\d{2}:\d{2}|\[\d{2}:\d{2}:\d{2}\])")
LOGLEVEL_RE = re.compile(r"\b(DEBUG|INFO|WARN|WARNING|ERROR|CRITICAL)\b", re.I)
PREFIX_RE = re.compile(r"^\s*([\[\+\*\-\>\#]+)\s?")
HEADER_RE = re.compile(r"^(=|-){3,}")
JSONXML_RE = re.compile(r"^\s*([\[{<]|<\?xml)")


def boundary_score(line: str) -> int:
    score = 0
    if not line.strip():
        score += 1
    if TIMESTAMP_RE.search(line):
        score += 1
    if LOGLEVEL_RE.search(line):
        score += 1
    if PREFIX_RE.search(line):
        score += 1
    if HEADER_RE.search(line):
        score += 1
    if JSONXML_RE.search(line):
        score += 1
    return score


def compute_topic_key(tool: str, meta: Dict[str, str]) -> str:
    key_parts = [tool or "", meta.get("host", ""), meta.get("ip", ""), meta.get("url_origin", ""), str(meta.get("port", "")), meta.get("file", "")]
    raw = "|".join(key_parts)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def split_text_to_chunks(
    text: str,
    *,
    tool: str = "",
    max_tokens: int = 800,
    min_tokens: int = 600,
    base_meta: Dict[str, str] | None = None,
) -> List[Tuple[int, int]]:
    """Return list of (byte_offset_start, byte_offset_end) chunk boundaries.

    Strategy: accumulate lines until reaching ~min_tokens and allow a boundary
    when boundary_score >= 2 or we exceed max_tokens.
    """
    base_meta = base_meta or {}
    lines = text.splitlines(keepends=True)
    offsets: List[int] = []
    pos = 0
    for ln in lines:
        offsets.append(pos)
        pos += len(ln.encode("utf-8"))
    offsets.append(pos)

    chunks: List[Tuple[int, int]] = []
    start_idx = 0
    acc_text: List[str] = []
    acc_tokens = 0
    for i, ln in enumerate(lines):
        acc_text.append(ln)
        acc_tokens = count_tokens("".join(acc_text))
        at_boundary = boundary_score(ln) >= 2
        over = acc_tokens >= max_tokens
        if (acc_tokens >= min_tokens and at_boundary) or over:
            start = offsets[start_idx]
            end = offsets[i + 1]
            if end > start:
                chunks.append((start, end))
            start_idx = i + 1
            acc_text = []
            acc_tokens = 0

    # Tail
    if start_idx < len(lines):
        start = offsets[start_idx]
        end = offsets[-1]
        if end > start:
            chunks.append((start, end))

    return chunks

