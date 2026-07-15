"""Centralized target extraction and resolution helpers for graph memory paths.

This module is the single authority for resolving concrete targets from free-form
text, planner metadata, conversation history, and working-memory referents.
Keeping this logic here prevents regex/heuristic drift across reducers and
subgraph planners.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit, urlunsplit
from typing import Any, Mapping, Sequence

_IPV4_OCTET_PATTERN = r"(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)"
_IPV4_PATTERN = re.compile(rf"\b{_IPV4_OCTET_PATTERN}(?:\.{_IPV4_OCTET_PATTERN}){{3}}\b")
_IPV4_CIDR_PATTERN = re.compile(
    rf"\b{_IPV4_OCTET_PATTERN}(?:\.{_IPV4_OCTET_PATTERN}){{3}}/(?:3[0-2]|[12]?\d)\b"
)
_HOSTNAME_PATTERN = re.compile(
    r"\b(?=.{1,255}\b)(?:[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?\.)+[a-zA-Z]{2,63}\b"
)
_LOCALHOST_PATTERN = re.compile(r"\blocalhost(?::\d{1,5})?\b", re.IGNORECASE)
_URL_PATTERN = re.compile(r"\bhttps?://[^\s\"'<>]+", re.IGNORECASE)
_WINDOWS_PATH_PATTERN = re.compile(r"^(?:[a-zA-Z]:\\|\\\\)[^\s\"'<>|]+$")
_UNIX_PATH_PATTERN = re.compile(r"^/[^\s\"'<>]+$")
_RELATIVE_PATH_PATTERN = re.compile(r"^\.\.?/[^\s\"'<>]+$")
_HOST_LABEL_PATTERN = re.compile(r"^[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?$")

TARGET_KIND_IP = "ip"
TARGET_KIND_CIDR = "cidr"
TARGET_KIND_HOSTNAME = "hostname"
TARGET_KIND_URL = "url"
TARGET_KIND_FILE_PATH = "file_path"
TARGET_KIND_URL_PATH = "url_path"

_ALL_TARGET_KINDS = frozenset(
    {
        TARGET_KIND_IP,
        TARGET_KIND_CIDR,
        TARGET_KIND_HOSTNAME,
        TARGET_KIND_URL,
        TARGET_KIND_FILE_PATH,
        TARGET_KIND_URL_PATH,
    }
)
_PRECISE_TARGET_KINDS = frozenset(
    {
        TARGET_KIND_IP,
        TARGET_KIND_CIDR,
        TARGET_KIND_HOSTNAME,
        TARGET_KIND_URL,
        TARGET_KIND_FILE_PATH,
        TARGET_KIND_URL_PATH,
    }
)
_SENSITIVE_MARKERS = ("token", "password", "secret", "api_key", "authorization", "cookie", "bearer")
_AMBIGUOUS_SINGLE_LABELS = frozenset(
    {
        "scan",
        "run",
        "check",
        "probe",
        "test",
        "target",
        "host",
        "hostname",
        "url",
        "path",
        "file",
        "it",
        "this",
        "that",
        "nmap",
    }
)
TargetFieldSpec = tuple[str, tuple[str, ...] | None, bool]
DEFAULT_TARGET_FIELD_SPECS: tuple[TargetFieldSpec, ...] = (
    ("target", None, True),
    ("host", (TARGET_KIND_HOSTNAME, TARGET_KIND_IP, TARGET_KIND_CIDR), True),
    ("ip", (TARGET_KIND_IP, TARGET_KIND_CIDR), False),
    ("url", (TARGET_KIND_URL,), False),
    ("path", (TARGET_KIND_FILE_PATH, TARGET_KIND_URL_PATH), False),
    ("file", (TARGET_KIND_FILE_PATH,), False),
    ("value", None, False),
)
RUNTIME_TOOL_TARGET_FIELD_SPECS: tuple[TargetFieldSpec, ...] = (
    ("target", None, True),
    ("host", (TARGET_KIND_HOSTNAME, TARGET_KIND_IP, TARGET_KIND_CIDR), True),
    ("ip", (TARGET_KIND_IP, TARGET_KIND_CIDR), False),
    ("address", (TARGET_KIND_HOSTNAME, TARGET_KIND_IP, TARGET_KIND_CIDR), True),
    ("url", (TARGET_KIND_URL,), False),
    ("uri", (TARGET_KIND_URL,), False),
    ("endpoint", (TARGET_KIND_URL, TARGET_KIND_URL_PATH), False),
    ("path", (TARGET_KIND_FILE_PATH, TARGET_KIND_URL_PATH), False),
    ("file", (TARGET_KIND_FILE_PATH,), False),
    ("filepath", (TARGET_KIND_FILE_PATH,), False),
    ("filename", (TARGET_KIND_FILE_PATH,), False),
)


def _as_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    return {}


def _normalize_allowed_kinds(allowed_kinds: Sequence[str] | None) -> frozenset[str] | None:
    if allowed_kinds is None:
        return None
    normalized = {str(item).strip().lower() for item in allowed_kinds if str(item).strip()}
    if not normalized:
        return frozenset()
    return frozenset(kind for kind in normalized if kind in _ALL_TARGET_KINDS)


def _normalize_field_specs(
    field_specs: Sequence[TargetFieldSpec] | None,
) -> tuple[tuple[str, frozenset[str] | None, bool], ...]:
    raw_specs = field_specs if field_specs is not None else DEFAULT_TARGET_FIELD_SPECS
    normalized_specs: list[tuple[str, frozenset[str] | None, bool]] = []
    has_value_field = False
    for raw_spec in raw_specs:
        if not isinstance(raw_spec, tuple) or len(raw_spec) != 3:
            continue
        raw_key, raw_allowed_kinds, raw_allow_single_label = raw_spec
        key = str(raw_key).strip()
        if not key:
            continue
        normalized_allowed_kinds = _normalize_allowed_kinds(raw_allowed_kinds)
        allow_single_label = bool(raw_allow_single_label)
        normalized_specs.append((key, normalized_allowed_kinds, allow_single_label))
        if key == "value":
            has_value_field = True

    # Keep nested {"value": "..."} payloads parseable even when custom specs are passed.
    if not has_value_field:
        normalized_specs.append(("value", None, False))
    return tuple(normalized_specs)


def _merge_allowed_kinds(
    left: frozenset[str] | None,
    right: frozenset[str] | None,
) -> frozenset[str] | None:
    if left is None:
        return right
    if right is None:
        return left
    return left & right


def _is_kind_allowed(kind: str, allowed_kinds: frozenset[str] | None) -> bool:
    if allowed_kinds is None:
        return True
    return kind in allowed_kinds


def _strip_trailing_punctuation(token: str) -> str:
    return token.strip().rstrip(",;:!?)]}\"'")


def _strip_url_query_fragment(path_text: str) -> str:
    without_query = path_text.split("?", 1)[0]
    return without_query.split("#", 1)[0]


def _contains_sensitive_marker(text: str) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in _SENSITIVE_MARKERS)


def _format_candidate(
    *,
    kind: str,
    value: str,
    confidence: float,
    allowed_kinds: frozenset[str] | None,
) -> dict[str, Any] | None:
    normalized_value = value.strip()
    if not normalized_value:
        return None
    if not _is_kind_allowed(kind, allowed_kinds):
        return None
    return {"value": normalized_value, "kind": kind, "confidence": confidence}


def _normalize_url_candidate(raw_url: str) -> str | None:
    cleaned = _strip_trailing_punctuation(raw_url.strip())
    if not cleaned:
        return None
    parsed = urlsplit(cleaned)
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"}:
        return None
    if not parsed.netloc:
        return None
    normalized_path = parsed.path or ""
    return urlunsplit((scheme, parsed.netloc, normalized_path, "", ""))


def _classify_slash_path(token: str, allowed_kinds: frozenset[str] | None) -> str:
    if "?" in token or "#" in token or "&" in token or "=" in token:
        return TARGET_KIND_URL_PATH
    if token.startswith("/api/") or token.startswith("/v1/") or token.startswith("/v2/"):
        return TARGET_KIND_URL_PATH
    if allowed_kinds is not None:
        if TARGET_KIND_URL_PATH in allowed_kinds and TARGET_KIND_FILE_PATH not in allowed_kinds:
            return TARGET_KIND_URL_PATH
        if TARGET_KIND_FILE_PATH in allowed_kinds and TARGET_KIND_URL_PATH not in allowed_kinds:
            return TARGET_KIND_FILE_PATH
    return TARGET_KIND_FILE_PATH


def _coerce_single_token(
    token: str,
    *,
    allow_single_label: bool,
    allowed_kinds: frozenset[str] | None,
) -> dict[str, Any] | None:
    normalized = _strip_trailing_punctuation(token)
    if not normalized:
        return None

    normalized_url = _normalize_url_candidate(normalized)
    if normalized_url:
        candidate = _format_candidate(
            kind=TARGET_KIND_URL,
            value=normalized_url,
            confidence=0.98,
            allowed_kinds=allowed_kinds,
        )
        if candidate:
            return candidate

    cidr_match = _IPV4_CIDR_PATTERN.fullmatch(normalized)
    if cidr_match:
        candidate = _format_candidate(
            kind=TARGET_KIND_CIDR,
            value=cidr_match.group(0),
            confidence=0.99,
            allowed_kinds=allowed_kinds,
        )
        if candidate:
            return candidate

    ip_match = _IPV4_PATTERN.fullmatch(normalized)
    if ip_match:
        candidate = _format_candidate(
            kind=TARGET_KIND_IP,
            value=ip_match.group(0),
            confidence=0.99,
            allowed_kinds=allowed_kinds,
        )
        if candidate:
            return candidate

    hostname_match = _HOSTNAME_PATTERN.fullmatch(normalized)
    if hostname_match:
        candidate = _format_candidate(
            kind=TARGET_KIND_HOSTNAME,
            value=hostname_match.group(0),
            confidence=0.95,
            allowed_kinds=allowed_kinds,
        )
        if candidate:
            return candidate

    localhost_match = _LOCALHOST_PATTERN.fullmatch(normalized)
    if localhost_match:
        candidate = _format_candidate(
            kind=TARGET_KIND_HOSTNAME,
            value=localhost_match.group(0),
            confidence=0.9,
            allowed_kinds=allowed_kinds,
        )
        if candidate:
            return candidate

    if _WINDOWS_PATH_PATTERN.fullmatch(normalized) or _RELATIVE_PATH_PATTERN.fullmatch(normalized):
        candidate = _format_candidate(
            kind=TARGET_KIND_FILE_PATH,
            value=normalized,
            confidence=0.9,
            allowed_kinds=allowed_kinds,
        )
        if candidate:
            return candidate

    if _UNIX_PATH_PATTERN.fullmatch(normalized):
        kind = _classify_slash_path(normalized, allowed_kinds)
        value = _strip_url_query_fragment(normalized) if kind == TARGET_KIND_URL_PATH else normalized
        candidate = _format_candidate(
            kind=kind,
            value=value,
            confidence=0.8,
            allowed_kinds=allowed_kinds,
        )
        if candidate:
            return candidate

    if (
        allow_single_label
        and _HOST_LABEL_PATTERN.fullmatch(normalized)
        and normalized.lower() not in _AMBIGUOUS_SINGLE_LABELS
        and not _contains_sensitive_marker(normalized)
    ):
        return _format_candidate(
            kind=TARGET_KIND_HOSTNAME,
            value=normalized,
            confidence=0.55,
            allowed_kinds=allowed_kinds,
        )

    return None


def _coerce_target_from_text(
    text: str,
    *,
    allow_single_label: bool,
    allowed_kinds: frozenset[str] | None,
) -> dict[str, Any] | None:
    normalized_text = text.strip()
    if not normalized_text:
        return None

    if " " not in normalized_text and "\n" not in normalized_text and "\t" not in normalized_text:
        return _coerce_single_token(
            normalized_text,
            allow_single_label=allow_single_label,
            allowed_kinds=allowed_kinds,
        )

    url_match = _URL_PATTERN.search(normalized_text)
    if url_match:
        candidate = _coerce_single_token(
            url_match.group(0),
            allow_single_label=False,
            allowed_kinds=allowed_kinds,
        )
        if candidate:
            return candidate

    cidr_match = _IPV4_CIDR_PATTERN.search(normalized_text)
    if cidr_match:
        candidate = _format_candidate(
            kind=TARGET_KIND_CIDR,
            value=cidr_match.group(0),
            confidence=0.99,
            allowed_kinds=allowed_kinds,
        )
        if candidate:
            return candidate

    ip_match = _IPV4_PATTERN.search(normalized_text)
    if ip_match:
        candidate = _format_candidate(
            kind=TARGET_KIND_IP,
            value=ip_match.group(0),
            confidence=0.99,
            allowed_kinds=allowed_kinds,
        )
        if candidate:
            return candidate

    hostname_match = _HOSTNAME_PATTERN.search(normalized_text)
    if hostname_match:
        candidate = _format_candidate(
            kind=TARGET_KIND_HOSTNAME,
            value=hostname_match.group(0),
            confidence=0.95,
            allowed_kinds=allowed_kinds,
        )
        if candidate:
            return candidate

    localhost_match = _LOCALHOST_PATTERN.search(normalized_text)
    if localhost_match:
        candidate = _format_candidate(
            kind=TARGET_KIND_HOSTNAME,
            value=localhost_match.group(0),
            confidence=0.9,
            allowed_kinds=allowed_kinds,
        )
        if candidate:
            return candidate

    return None


def extract_target_token(text: str) -> str | None:
    """Extract the first concrete target token (CIDR/IP/hostname) from text."""
    if not isinstance(text, str):
        return None
    candidate_text = text.strip()
    if not candidate_text:
        return None

    cidr_match = _IPV4_CIDR_PATTERN.search(candidate_text)
    if cidr_match:
        return cidr_match.group(0)

    ip_match = _IPV4_PATTERN.search(candidate_text)
    if ip_match:
        return ip_match.group(0)

    hostname_match = _HOSTNAME_PATTERN.search(candidate_text)
    if hostname_match:
        return hostname_match.group(0)

    return None


def coerce_target_candidate(
    value: Any,
    *,
    allow_single_label: bool = False,
    allowed_kinds: Sequence[str] | None = None,
    field_specs: Sequence[TargetFieldSpec] | None = None,
) -> dict[str, Any] | None:
    """Normalize payloads into a typed target candidate when possible."""
    normalized_allowed = _normalize_allowed_kinds(allowed_kinds)

    if isinstance(value, Mapping):
        payload = _as_mapping(value)
        for key, hinted_kinds, allow_single_for_key in _normalize_field_specs(field_specs):
            if key not in payload:
                continue
            allowed_for_key = _merge_allowed_kinds(normalized_allowed, hinted_kinds)
            if allowed_for_key == frozenset():
                continue
            candidate = coerce_target_candidate(
                payload.get(key),
                allow_single_label=allow_single_label or allow_single_for_key,
                allowed_kinds=allowed_for_key,
                field_specs=field_specs,
            )
            if candidate:
                return candidate
        return None

    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        fallback_candidate: dict[str, Any] | None = None
        for item in value:
            candidate = coerce_target_candidate(
                item,
                allow_single_label=allow_single_label,
                allowed_kinds=normalized_allowed,
                field_specs=field_specs,
            )
            if candidate:
                if str(candidate.get("kind")) in _PRECISE_TARGET_KINDS:
                    return candidate
                if fallback_candidate is None:
                    fallback_candidate = candidate
        if fallback_candidate is not None:
            return fallback_candidate
        return None

    if not isinstance(value, str):
        return None

    return _coerce_target_from_text(
        value,
        allow_single_label=allow_single_label,
        allowed_kinds=normalized_allowed,
    )


def coerce_target_value(
    value: Any,
    *,
    allow_single_label: bool = False,
    allowed_kinds: Sequence[str] | None = None,
    field_specs: Sequence[TargetFieldSpec] | None = None,
) -> str | None:
    """Normalize arbitrary payloads into a concrete target string when possible."""
    candidate = coerce_target_candidate(
        value,
        allow_single_label=allow_single_label,
        allowed_kinds=allowed_kinds,
        field_specs=field_specs,
    )
    if not candidate:
        return None
    raw_value = candidate.get("value")
    if isinstance(raw_value, str) and raw_value.strip():
        return raw_value.strip()
    return None


def resolve_target_from_history(history: Sequence[Any], *, max_entries: int = 8) -> str | None:
    """Resolve a concrete target from recent history entries."""
    if not history:
        return None

    for entry in reversed(list(history)[-max_entries:]):
        if not isinstance(entry, Mapping):
            continue
        for key in ("content", "content_excerpt", "message"):
            candidate = coerce_target_value(entry.get(key))
            if candidate:
                return candidate
    return None


def resolve_target_from_working_memory(
    working_memory: Mapping[str, Any],
    *,
    intent_referent_key: str = "intent:target",
    recent_turn_limit: int = 4,
) -> str | None:
    """Resolve a concrete target from canonical working-memory structures."""
    wm = _as_mapping(working_memory)
    if not wm:
        return None

    referents = _as_mapping(wm.get("referents"))
    active = _as_mapping(wm.get("active"))
    active_target_id = active.get("target_id")
    active_referent_key = ""
    if isinstance(active_target_id, str):
        active_referent_key = active_target_id.strip()
        if active_referent_key.startswith("target:"):
            active_referent_key = active_referent_key[len("target:") :]

    prioritized_keys: list[str] = []
    if active_referent_key:
        prioritized_keys.append(active_referent_key)
    if intent_referent_key not in prioritized_keys:
        prioritized_keys.append(intent_referent_key)

    for referent_key in prioritized_keys:
        candidate = coerce_target_value(referents.get(referent_key), allow_single_label=True)
        if candidate:
            return candidate

    for referent_value in referents.values():
        candidate = coerce_target_value(referent_value, allow_single_label=True)
        if candidate:
            return candidate

    recent_turns = wm.get("recent_turns")
    if isinstance(recent_turns, list):
        for turn in reversed(recent_turns[-recent_turn_limit:]):
            if not isinstance(turn, Mapping):
                continue
            candidate = coerce_target_value(turn.get("content"))
            if candidate:
                return candidate
            candidate = coerce_target_value(turn.get("content_excerpt"))
            if candidate:
                return candidate

    input_payload = _as_mapping(wm.get("input"))
    candidate = coerce_target_value(input_payload.get("user_message_excerpt"))
    if candidate:
        return candidate

    return None


def resolve_active_target_from_working_memory(working_memory: Mapping[str, Any]) -> str | None:
    """Resolve only the currently bound active target from canonical working memory.

    Unlike ``resolve_target_from_working_memory``, this helper does not scan
    recent turns or input excerpts. It only resolves ``active.target_id``
    against referents to represent the active target binding for this turn.
    """
    wm = _as_mapping(working_memory)
    if not wm:
        return None

    referents = _as_mapping(wm.get("referents"))
    active = _as_mapping(wm.get("active"))
    active_target_id = active.get("target_id")
    if not isinstance(active_target_id, str):
        return None

    active_referent_key = active_target_id.strip()
    if not active_referent_key:
        return None
    if active_referent_key.startswith("target:"):
        active_referent_key = active_referent_key[len("target:") :]
    if not active_referent_key:
        return None

    return coerce_target_value(referents.get(active_referent_key), allow_single_label=True)


def resolve_planner_target(
    *,
    user_message: str,
    request_targets: Sequence[str],
    metadata: Mapping[str, Any],
    history: Sequence[Any],
    tool_intent: Mapping[str, Any],
) -> str:
    """Resolve planner target using structured continuity and canonical bindings.

    Fallback order (first non-empty wins):
    1. ``metadata["intent_target_resolution"]`` when status is ``resolved``
       -- classifier is authoritative when it has an answer.
    2. Canonical active target binding from working memory, but only when
       ``metadata["intent_target_continuity"].status == "allow"``.
    3. Canonical working-memory referents/recent turns when no explicit
       continuity decision exists.
    4. Explicit ``request_targets`` from the initial request.
    5. ``tool_intent.target`` from post-tool reasoning.
    """
    del history  # reserved for signature compatibility; continuity is structured metadata.
    del user_message

    # 1. Classifier-resolved target (authoritative)
    intent_resolution = metadata.get("intent_target_resolution")
    if isinstance(intent_resolution, Mapping):
        status = str(intent_resolution.get("target_status") or "").strip().lower()
        resolved_target = coerce_target_value(intent_resolution.get("resolved_target"))
        if status == "resolved" and resolved_target:
            return resolved_target

    # 2. Continuity-authorized active target from canonical working memory.
    continuity = metadata.get("intent_target_continuity")
    continuity_status = ""
    if isinstance(continuity, Mapping):
        continuity_status = str(continuity.get("status") or "").strip().lower()
    working_memory = metadata.get("working_memory")
    if continuity_status == "allow" and isinstance(working_memory, Mapping):
        wm_target = resolve_active_target_from_working_memory(working_memory)
        if wm_target:
            return wm_target
    if not continuity_status and isinstance(working_memory, Mapping):
        wm_target = resolve_target_from_working_memory(working_memory)
        if wm_target:
            return wm_target

    # 4. Explicit request targets
    for target_candidate in (request_targets or []):
        coerced = coerce_target_value(target_candidate)
        if coerced:
            return coerced

    # 5. tool_intent.target from post-tool reasoning
    if isinstance(tool_intent, Mapping):
        intent_target = coerce_target_value(tool_intent.get("target"))
        if intent_target:
            return intent_target

    return ""


__all__ = [
    "TargetFieldSpec",
    "DEFAULT_TARGET_FIELD_SPECS",
    "RUNTIME_TOOL_TARGET_FIELD_SPECS",
    "extract_target_token",
    "coerce_target_candidate",
    "coerce_target_value",
    "resolve_active_target_from_working_memory",
    "resolve_planner_target",
    "resolve_target_from_history",
    "resolve_target_from_working_memory",
]
