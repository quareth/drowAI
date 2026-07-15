"""Family-level HTTP response analysis.

This module normalizes already-parsed HTTP request/download metadata into a
small reusable response fact model. It performs shallow HTML/header extraction
for browser-inspection facts, but avoids artifact reads, runtime calls, backend
imports, compression contracts, vulnerability claims, or LLM behavior.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from html.parser import HTMLParser
from http.cookies import SimpleCookie
import re
from typing import Any, Optional
from urllib.parse import urljoin, urlsplit, urlunsplit


_DOWNLOAD_EXTENSIONS = frozenset(
    (
        ".7z",
        ".bak",
        ".csv",
        ".db",
        ".gz",
        ".json",
        ".log",
        ".pcap",
        ".rar",
        ".sql",
        ".tar",
        ".tgz",
        ".txt",
        ".xml",
        ".zip",
    )
)
_DOWNLOAD_PATH_MARKERS = frozenset(("backup", "download", "dump", "export", "report"))
_NAVIGATION_REF_ATTRIBUTES = frozenset(
    (
        "data-action",
        "data-href",
        "data-link",
        "data-url",
        "formaction",
    )
)
_STATIC_JS_NAVIGATION_RE = re.compile(
    r"""
    (?:
        (?:(?:window|document)\.)?location(?:\.href)?\s*=\s*
        |
        (?:(?:window|document)\.)?location\.(?:assign|replace)\s*\(\s*
    )
    (?P<quote>["'])
    (?P<ref>[^"'<>\\]+)
    (?P=quote)
    """,
    re.IGNORECASE | re.VERBOSE,
)


@dataclass(frozen=True)
class WebResponseForm:
    """Normalized HTML form facts."""

    method: str
    action: Optional[str]
    input_names: tuple[str, ...]
    input_types: tuple[str, ...]


@dataclass(frozen=True)
class WebResponseCookie:
    """Normalized cookie metadata without cookie values."""

    name: str
    flags: tuple[str, ...]
    path: Optional[str] = None
    domain: Optional[str] = None


@dataclass(frozen=True)
class WebResponseAnalysis:
    """Normalized metadata facts for one HTTP response-oriented tool result."""

    source_tool: str
    method: str
    url: Optional[str]
    status_code: Optional[int]
    content_type: Optional[str]
    content_length: Optional[int]
    redirect_count: int
    body_truncated: bool
    body_captured: bool
    response_mode: Optional[str]
    saved_path: Optional[str] = None
    bytes_written: Optional[int] = None
    sha256: Optional[str] = None
    checksum_verified: Optional[bool] = None
    title: Optional[str] = None
    headings: tuple[str, ...] = ()
    internal_links: tuple[str, ...] = ()
    external_links: tuple[str, ...] = ()
    asset_refs: tuple[str, ...] = ()
    script_srcs: tuple[str, ...] = ()
    stylesheet_refs: tuple[str, ...] = ()
    image_refs: tuple[str, ...] = ()
    download_links: tuple[str, ...] = ()
    forms: tuple[WebResponseForm, ...] = ()
    cookies: tuple[WebResponseCookie, ...] = ()
    tech_hints: tuple[str, ...] = ()
    body_line_count: Optional[int] = None


def analyze_http_request_response(
    *,
    source_tool: str,
    metadata: Mapping[str, Any],
    parameters: Mapping[str, Any],
    response_text: Optional[str] = None,
    safe_url: Optional[Callable[[Optional[str]], Optional[str]]] = None,
) -> WebResponseAnalysis:
    """Return metadata and shallow browser-inspection facts for an http_request result."""

    method = _first_text(metadata.get("request_method"), parameters.get("method")) or "GET"
    raw_url = _first_text(metadata.get("effective_url"), parameters.get("target"))
    body_text = _captured_response_body(response_text=response_text, metadata=metadata)
    html_facts = _analyze_html(
        body_text,
        base_url=raw_url,
        safe_url=safe_url,
    )
    headers = _mapping_or_empty(metadata.get("response_headers"))
    return WebResponseAnalysis(
        source_tool=source_tool,
        method=method,
        url=_sanitize_url(raw_url, safe_url=safe_url),
        status_code=_as_int(metadata.get("status_code")),
        content_type=_first_text(metadata.get("content_type")),
        content_length=_as_int(metadata.get("content_length")),
        redirect_count=_as_int(metadata.get("redirect_count")) or 0,
        body_truncated=bool(metadata.get("body_truncated")),
        body_captured=bool(metadata.get("body_captured")),
        response_mode=_first_text(metadata.get("response_mode")),
        title=html_facts["title"],
        headings=html_facts["headings"],
        internal_links=html_facts["internal_links"],
        external_links=html_facts["external_links"],
        asset_refs=html_facts["asset_refs"],
        script_srcs=html_facts["script_srcs"],
        stylesheet_refs=html_facts["stylesheet_refs"],
        image_refs=html_facts["image_refs"],
        download_links=html_facts["download_links"],
        forms=html_facts["forms"],
        cookies=_cookies_from_headers(headers),
        tech_hints=_tech_hints(headers=headers, html_facts=html_facts),
        body_line_count=_body_line_count(body_text),
    )


def analyze_http_download_response(
    *,
    source_tool: str,
    metadata: Mapping[str, Any],
    parameters: Mapping[str, Any],
    safe_url: Optional[Callable[[Optional[str]], Optional[str]]] = None,
    safe_workspace_path: Optional[Callable[[Optional[str]], Optional[str]]] = None,
) -> WebResponseAnalysis:
    """Return current metadata-level facts for an http_download result."""

    raw_url = _first_text(metadata.get("effective_url"), parameters.get("target"))
    raw_saved_path = _first_text(metadata.get("saved_path"), parameters.get("output_path"))
    saved_path = (
        safe_workspace_path(raw_saved_path)
        if safe_workspace_path is not None
        else raw_saved_path
    )
    checksum_verified = metadata.get("checksum_verified")
    if not isinstance(checksum_verified, bool):
        checksum_verified = None
    return WebResponseAnalysis(
        source_tool=source_tool,
        method="GET",
        url=_sanitize_url(raw_url, safe_url=safe_url),
        status_code=_as_int(metadata.get("status_code")),
        content_type=_first_text(metadata.get("content_type")),
        content_length=_as_int(metadata.get("content_length")),
        redirect_count=_as_int(metadata.get("redirect_count")) or 0,
        body_truncated=False,
        body_captured=False,
        response_mode=_first_text(metadata.get("response_mode")),
        saved_path=saved_path,
        bytes_written=_as_int(metadata.get("bytes_written")),
        sha256=_first_text(metadata.get("sha256")),
        checksum_verified=checksum_verified,
    )


class _HtmlFactsParser(HTMLParser):
    """Collect shallow HTML facts without interpreting scripts or styles."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title_parts: list[str] = []
        self.heading_parts: list[str] = []
        self.headings: list[str] = []
        self.links: list[str] = []
        self.navigation_refs: list[str] = []
        self.script_srcs: list[str] = []
        self.stylesheet_refs: list[str] = []
        self.image_refs: list[str] = []
        self.asset_refs: list[str] = []
        self.download_hrefs: list[str] = []
        self.forms: list[dict[str, Any]] = []
        self.meta_generators: list[str] = []
        self._in_title = False
        self._in_heading = False
        self._current_form: Optional[dict[str, Any]] = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        lowered = tag.lower()
        attr_map = {name.lower(): value for name, value in attrs if name}
        self.navigation_refs.extend(_navigation_refs_from_attributes(attr_map))

        if lowered == "title":
            self._in_title = True
        elif lowered in {"h1", "h2"}:
            self._in_heading = True
            self.heading_parts = []
        elif lowered == "a":
            href = _text_or_none(attr_map.get("href"))
            if href:
                self.links.append(href)
                if "download" in attr_map:
                    self.download_hrefs.append(href)
        elif lowered == "script":
            src = _text_or_none(attr_map.get("src"))
            if src:
                self.script_srcs.append(src)
                self.asset_refs.append(src)
        elif lowered == "link":
            href = _text_or_none(attr_map.get("href"))
            rel = str(attr_map.get("rel") or "").lower()
            if href:
                self.asset_refs.append(href)
                if "stylesheet" in rel or href.lower().split("?", 1)[0].endswith(".css"):
                    self.stylesheet_refs.append(href)
        elif lowered == "img":
            src = _text_or_none(attr_map.get("src"))
            if src:
                self.image_refs.append(src)
                self.asset_refs.append(src)
        elif lowered == "form":
            self._current_form = {
                "method": (_text_or_none(attr_map.get("method")) or "GET").upper(),
                "action": _text_or_none(attr_map.get("action")),
                "input_names": [],
                "input_types": [],
            }
        elif lowered == "input" and self._current_form is not None:
            input_name = _text_or_none(attr_map.get("name"))
            input_type = (_text_or_none(attr_map.get("type")) or "text").lower()
            if input_name:
                self._current_form["input_names"].append(input_name)
            self._current_form["input_types"].append(input_type)
        elif lowered == "meta":
            name = str(attr_map.get("name") or attr_map.get("property") or "").lower()
            content = _text_or_none(attr_map.get("content"))
            if name == "generator" and content:
                self.meta_generators.append(content)

    def handle_endtag(self, tag: str) -> None:
        lowered = tag.lower()
        if lowered == "title":
            self._in_title = False
        elif lowered in {"h1", "h2"} and self._in_heading:
            heading = _normalize_inline_text(" ".join(self.heading_parts))
            if heading:
                self.headings.append(heading)
            self._in_heading = False
            self.heading_parts = []
        elif lowered == "form" and self._current_form is not None:
            self.forms.append(self._current_form)
            self._current_form = None

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title_parts.append(data)
        if self._in_heading:
            self.heading_parts.append(data)


def _analyze_html(
    body_text: Optional[str],
    *,
    base_url: Optional[str],
    safe_url: Optional[Callable[[Optional[str]], Optional[str]]],
) -> dict[str, Any]:
    parser = _HtmlFactsParser()
    if body_text:
        try:
            parser.feed(body_text)
        except Exception:
            pass

    internal_links, external_links = _partition_refs(
        [*parser.links, *parser.navigation_refs],
        base_url=base_url,
        safe_url=safe_url,
    )
    asset_refs, _ = _partition_refs(
        parser.asset_refs,
        base_url=base_url,
        safe_url=safe_url,
    )
    script_srcs, external_scripts = _partition_refs(
        parser.script_srcs,
        base_url=base_url,
        safe_url=safe_url,
    )
    stylesheet_refs, external_styles = _partition_refs(
        parser.stylesheet_refs,
        base_url=base_url,
        safe_url=safe_url,
    )
    image_refs, external_images = _partition_refs(
        parser.image_refs,
        base_url=base_url,
        safe_url=safe_url,
    )

    download_links = _download_links(
        [*parser.links, *parser.navigation_refs],
        parser.download_hrefs,
        base_url=base_url,
        safe_url=safe_url,
    )
    forms = tuple(
        WebResponseForm(
            method=str(form.get("method") or "GET").upper(),
            action=_normalize_ref(
                form.get("action"),
                base_url=base_url,
                safe_url=safe_url,
                keep_external=True,
            ),
            input_names=tuple(_dedupe(form.get("input_names") or ())),
            input_types=tuple(_dedupe(form.get("input_types") or ())),
        )
        for form in parser.forms
    )

    return {
        "title": _normalize_inline_text(" ".join(parser.title_parts)) or None,
        "headings": tuple(_dedupe(parser.headings)),
        "internal_links": tuple(internal_links),
        "external_links": tuple(external_links),
        "asset_refs": tuple(_dedupe((*asset_refs, *external_scripts, *external_styles, *external_images))),
        "script_srcs": tuple(_dedupe((*script_srcs, *external_scripts))),
        "stylesheet_refs": tuple(_dedupe((*stylesheet_refs, *external_styles))),
        "image_refs": tuple(_dedupe((*image_refs, *external_images))),
        "download_links": tuple(download_links),
        "forms": forms,
        "meta_generators": tuple(_dedupe(parser.meta_generators)),
    }


def _navigation_refs_from_attributes(attr_map: Mapping[str, Optional[str]]) -> list[str]:
    """Extract static navigation targets from URL-bearing attributes."""

    refs: list[str] = []
    for name, value in attr_map.items():
        text = _text_or_none(value)
        if not text:
            continue
        if name in _NAVIGATION_REF_ATTRIBUTES and _looks_like_static_ref(text):
            refs.append(text)
            continue
        if name.startswith("on"):
            refs.extend(_static_js_navigation_refs(text))
    return refs


def _static_js_navigation_refs(script: str) -> list[str]:
    """Return quoted static location targets from simple inline navigation JS."""

    refs: list[str] = []
    for match in _STATIC_JS_NAVIGATION_RE.finditer(script):
        ref = _text_or_none(match.group("ref"))
        if ref and _looks_like_static_ref(ref):
            refs.append(ref)
    return refs


def _looks_like_static_ref(value: str) -> bool:
    """Return true for static URL/path literals that can be normalized safely."""

    text = value.strip()
    if not text or text.startswith(("#", "javascript:", "mailto:", "tel:")):
        return False
    lowered = text.lower()
    if lowered.startswith(("http://", "https://", "/", "./", "../")):
        return True
    return bool(
        lowered
        and not any(char.isspace() for char in lowered)
        and not any(char in lowered for char in "<>{}()")
        and "/" in lowered
    )


def _partition_refs(
    refs: list[str],
    *,
    base_url: Optional[str],
    safe_url: Optional[Callable[[Optional[str]], Optional[str]]],
) -> tuple[list[str], list[str]]:
    internal: list[str] = []
    external: list[str] = []
    base_host = _host_key(base_url)
    for ref in refs:
        normalized = _normalize_ref(
            ref,
            base_url=base_url,
            safe_url=safe_url,
            keep_external=True,
        )
        if not normalized:
            continue
        absolute = _absolute_ref(ref, base_url=base_url)
        ref_host = _host_key(absolute)
        if ref_host and base_host and ref_host != base_host:
            external.append(normalized)
        else:
            internal.append(normalized)
    return _dedupe(internal), _dedupe(external)


def _download_links(
    hrefs: list[str],
    explicit_downloads: list[str],
    *,
    base_url: Optional[str],
    safe_url: Optional[Callable[[Optional[str]], Optional[str]]],
) -> tuple[str, ...]:
    candidates: list[str] = []
    explicit = set(explicit_downloads)
    for href in hrefs:
        normalized = _normalize_ref(
            href,
            base_url=base_url,
            safe_url=safe_url,
            keep_external=True,
        )
        if not normalized:
            continue
        if href in explicit or _looks_downloadable(normalized):
            candidates.append(normalized)
    return tuple(_dedupe(candidates))


def _looks_downloadable(ref: str) -> bool:
    lowered = ref.lower().split("?", 1)[0]
    if any(lowered.endswith(extension) for extension in _DOWNLOAD_EXTENSIONS):
        return True
    path_parts = [part for part in lowered.split("/") if part]
    return any(marker in path_parts for marker in _DOWNLOAD_PATH_MARKERS)


def _normalize_ref(
    ref: Any,
    *,
    base_url: Optional[str],
    safe_url: Optional[Callable[[Optional[str]], Optional[str]]],
    keep_external: bool,
) -> Optional[str]:
    text = _text_or_none(ref)
    if not text or text.startswith(("#", "javascript:", "mailto:", "tel:")):
        return None
    absolute = _absolute_ref(text, base_url=base_url)
    if not absolute:
        return _normalize_inline_text(text)

    parsed = urlsplit(absolute)
    base_host = _host_key(base_url)
    ref_host = _host_key(absolute)
    if parsed.scheme in {"http", "https"} and base_host and ref_host == base_host:
        path = parsed.path or "/"
        return urlunsplit(("", "", path, parsed.query, ""))
    if parsed.scheme in {"http", "https"}:
        return _sanitize_url(absolute, safe_url=safe_url) if keep_external else None
    return _normalize_inline_text(text)


def _absolute_ref(ref: str, *, base_url: Optional[str]) -> Optional[str]:
    try:
        return urljoin(base_url or "", ref)
    except ValueError:
        return None


def _host_key(url: Optional[str]) -> Optional[str]:
    if not url:
        return None
    try:
        parsed = urlsplit(url)
    except ValueError:
        return None
    if not parsed.hostname:
        return None
    port = parsed.port
    if port is None:
        port = 443 if parsed.scheme == "https" else 80 if parsed.scheme == "http" else None
    return f"{parsed.hostname.lower()}:{port}" if port is not None else parsed.hostname.lower()


def _cookies_from_headers(headers: Mapping[str, Any]) -> tuple[WebResponseCookie, ...]:
    values: list[str] = []
    for key, value in headers.items():
        if str(key).strip().lower() != "set-cookie":
            continue
        if isinstance(value, list):
            values.extend(str(item) for item in value if item is not None)
        elif value is not None:
            values.append(str(value))

    cookies: list[WebResponseCookie] = []
    seen: set[str] = set()
    for value in values:
        parsed = SimpleCookie()
        try:
            parsed.load(value)
        except Exception:
            continue
        for name, morsel in parsed.items():
            if name in seen:
                continue
            seen.add(name)
            flags: list[str] = []
            if morsel["secure"]:
                flags.append("Secure")
            if morsel["httponly"]:
                flags.append("HttpOnly")
            if morsel["samesite"]:
                flags.append(f"SameSite={morsel['samesite']}")
            cookies.append(
                WebResponseCookie(
                    name=name,
                    flags=tuple(flags),
                    path=_text_or_none(morsel["path"]),
                    domain=_text_or_none(morsel["domain"]),
                )
            )
    return tuple(cookies)


def _tech_hints(*, headers: Mapping[str, Any], html_facts: Mapping[str, Any]) -> tuple[str, ...]:
    hints: list[str] = []
    server = _header_value(headers, "server")
    powered_by = _header_value(headers, "x-powered-by")
    if server:
        hints.append(f"server={server}")
    if powered_by:
        hints.append(f"x-powered-by={powered_by}")
    for generator in html_facts.get("meta_generators") or ():
        hints.append(f"generator={generator}")
    for asset in html_facts.get("asset_refs") or ():
        lower = str(asset).lower()
        for marker, label in (
            ("bootstrap", "Bootstrap"),
            ("jquery", "jQuery"),
            ("chart", "Chart"),
            ("highcharts", "Highcharts"),
            ("zingchart", "ZingChart"),
        ):
            if marker in lower:
                hints.append(f"asset={label}")
    return tuple(_dedupe(hints))


def _extract_response_body(response_text: Optional[str]) -> Optional[str]:
    if not response_text:
        return None
    text = str(response_text).replace("\r\n", "\n")
    lines = text.split("\n")
    last_status_index: Optional[int] = None
    for index, line in enumerate(lines):
        if line.startswith("HTTP/"):
            last_status_index = index
    if last_status_index is None:
        return text
    for index in range(last_status_index + 1, len(lines)):
        if not lines[index].strip():
            return "\n".join(lines[index + 1 :])
    return text


def _captured_response_body(
    *,
    response_text: Optional[str],
    metadata: Mapping[str, Any],
) -> Optional[str]:
    if metadata.get("body_captured") is False and (_as_int(metadata.get("content_length")) or 0) == 0:
        return None
    if response_text is None:
        return None
    return _extract_response_body(response_text)


def _body_line_count(body_text: Optional[str]) -> Optional[int]:
    if body_text is None:
        return None
    return len([line for line in body_text.splitlines() if line.strip()])


def _sanitize_url(
    value: Optional[str],
    *,
    safe_url: Optional[Callable[[Optional[str]], Optional[str]]],
) -> Optional[str]:
    if not value:
        return None
    if safe_url is None:
        return value
    return safe_url(value)


def _mapping_or_empty(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _header_value(headers: Mapping[str, Any], header_name: str) -> Optional[str]:
    wanted = header_name.lower()
    for key, value in headers.items():
        if str(key).strip().lower() == wanted:
            return _first_text(value)
    return None


def _first_text(*values: Any) -> Optional[str]:
    for value in values:
        text = _text_or_none(value)
        if text:
            return text
    return None


def _text_or_none(value: Any) -> Optional[str]:
    text = str(value or "").strip()
    return text or None


def _normalize_inline_text(value: Any) -> str:
    return " ".join(str(value or "").split())


def _dedupe(values: Any) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = _normalize_inline_text(value)
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def _as_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
