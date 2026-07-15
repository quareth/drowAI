"""Optional artifact redactor for secrets/tokens with offset-safe masking.

This module provides a lightweight, configurable redaction function that masks
common secret patterns (bearer/JWT tokens, API keys, cookies, query params)
while preserving the original text length so byte-offset citations remain valid.

Enable via env var CONTEXT_ENABLE_ARTIFACT_REDACTION=true.
"""

from __future__ import annotations

import os
import re
from typing import List, Pattern, Tuple


def _compile_default_patterns() -> List[Tuple[str, Pattern[str]]]:
    """Return a list of (name, regex) for common secret tokens.

    Patterns are conservative to avoid over-redaction and focus on typical
    bearer/JWT/cookie/api-key usages found in logs and HTTP traces.
    """
    patterns: List[Tuple[str, Pattern[str]]] = []
    # Authorization: Bearer <token>
    patterns.append((
        "bearer",
        re.compile(r"(?i)(Authorization:\s*Bearer\s+)([A-Za-z0-9\-\._~=\+/]{10,})")
    ))
    # JWT-like tokens (3 base64url parts separated by dots, reasonably long)
    patterns.append((
        "jwt",
        re.compile(r"\b([A-Za-z0-9_-]{8,})\.([A-Za-z0-9_-]{8,})\.([A-Za-z0-9_-]{8,})\b")
    ))
    # Cookie or Set-Cookie session-like values
    patterns.append((
        "cookie",
        re.compile(r"(?i)((?:Set-)?Cookie:\s*[^=;\s]+\s*=\s*)([^;\r\n]{8,})")
    ))
    # Common API key header
    patterns.append((
        "api_key_header",
        re.compile(r"(?i)(X-API-Key:\s*)([A-Za-z0-9\-_]{8,})")
    ))
    # Query parameters that commonly carry tokens
    patterns.append((
        "query_token",
        re.compile(r"(?i)([?&](?:token|access_token|auth|apikey|api_key|key)=)([A-Za-z0-9\-\._]{6,})")
    ))
    # Generic key/value secret fields in text or JSON-like payloads
    patterns.append((
        "kv_secret",
        re.compile(
            r"(?i)((?:\"?(?:token|access_token|auth|apikey|api_key|secret|password)\"?\s*[:=]\s*\"?))"
            r"([A-Za-z0-9\-\._~=\+/]{6,})"
        )
    ))
    # AWS-like keys (very rough heuristics)
    patterns.append((
        "aws_access_key",
        re.compile(r"(?i)(AWS_ACCESS_KEY_ID\s*=\s*)([A-Z0-9]{8,})")
    ))
    patterns.append((
        "aws_secret",
        re.compile(r"(?i)(AWS_SECRET_ACCESS_KEY\s*=\s*)([A-Za-z0-9/+=]{16,})")
    ))
    return patterns


def _mask_same_length(s: str) -> str:
    """Return a same-length mask preserving word characters and punctuation distribution.

    We use '•' for letters/digits and keep non-alnum separators to improve readability.
    """
    out = []
    for ch in s:
        if ch.isalnum():
            out.append("•")
        else:
            # keep separators (e.g., '.', '-', '_', '=')
            out.append(ch)
    return "".join(out)


class ArtifactRedactor:
    def __init__(self, patterns: List[Tuple[str, Pattern[str]]] | None = None) -> None:
        self.patterns = patterns or _compile_default_patterns()

    def redact_equal_len(self, text: str) -> str:
        """Redact secrets by replacing values with same-length masks.

        For patterns with capture groups (prefix, secret), only the secret group
        is masked so context (e.g., "Authorization: Bearer ") is preserved.
        """
        if not text:
            return text
        out = text
        for name, rx in self.patterns:
            def _repl(m: re.Match[str]) -> str:
                # If there are 2+ groups, assume the last is the secret; keep prefixes
                g = m.groups()
                if not g:
                    return _mask_same_length(m.group(0))
                secret = g[-1]
                masked = _mask_same_length(secret)
                # Replace only first occurrence inside the matched span.
                return m.group(0).replace(secret, masked, 1)
            out = rx.sub(_repl, out)
        return out


def is_redaction_enabled() -> bool:
    return os.getenv("CONTEXT_ENABLE_ARTIFACT_REDACTION", "false").lower() == "true"

