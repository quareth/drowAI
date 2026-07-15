"""
Reusable parsing utilities for web application tools.

These helpers consolidate common parsing patterns observed across web
application tool integrations (crawlers, scanners, CMS identifiers, fuzzers,
proxies) and mirror the robust JSON line handling found in
`agent/tools/information_gathering/dns/amass.py` as well as the XML handling
approach in `agent/tools/information_gathering/network_discovery/nmap.py`.

Examples:
    # JSON parsing (single object)
    metadata = parse_json_output(stdout)

    # JSON parsing (line-by-line like amass)
    metadata = parse_json_output(stdout, line_by_line=True)

    # XML parsing (like nmap)
    metadata = parse_xml_output(stdout, root_element="nmaprun")

    # CSV parsing (ffuf-style)
    metadata = parse_csv_output(stdout, delimiter=",", has_header=True)

    # Vulnerability extraction (sqlmap/nikto style)
    vulns = extract_vulnerabilities(metadata.get("data", []))

    # Severity normalization
    severity = normalize_severity("high")   # Returns "High"
    severity = normalize_severity(8.5)      # Returns "High" (CVSS)
"""

from __future__ import annotations

import csv
import json
import logging
import re
import xml.etree.ElementTree as ET
from io import StringIO
from typing import Any, Dict, List, Optional, TypedDict, Union


logger = logging.getLogger(__name__)

# Standard severity levels
SEVERITY_CRITICAL = "Critical"
SEVERITY_HIGH = "High"
SEVERITY_MEDIUM = "Medium"
SEVERITY_LOW = "Low"
SEVERITY_INFO = "Info"

# Common vulnerability keys
VULN_KEYS: List[str] = ["vulnerabilities", "issues", "findings", "alerts", "results"]

# Output format detection patterns
FORMAT_PATTERNS = {
    "json": r"^\s*[\{\[]",
    "xml": r"^\s*<\?xml|^\s*<[a-zA-Z]",
    "csv": r"^[^,\n]+,[^,\n]+",
}


class JsonParseResult(TypedDict, total=False):
    data: List[Any]
    summary: Dict[str, Any]
    error: Optional[str]
    raw_output: str


class XmlParseResult(TypedDict, total=False):
    elements: List[Dict[str, Any]]
    attributes: Dict[str, Any]
    error: Optional[str]
    raw_output: str


class CsvParseResult(TypedDict, total=False):
    rows: List[Dict[str, Any]]
    headers: List[str]
    row_count: int
    error: Optional[str]
    raw_output: str


class VulnerabilityEntry(TypedDict, total=False):
    type: Optional[str]
    severity: str
    description: Optional[str]
    parameter: Optional[str]
    payload: Optional[str]
    location: Optional[str]
    raw: Any


def clean_output(
    output: str,
    *,
    strip_ansi: bool = True,
    strip_whitespace: bool = True,
    max_length: Optional[int] = None,
) -> str:
    """
    Clean and normalize raw tool output.

    Args:
        output: Raw output text from a tool.
        strip_ansi: Remove ANSI escape sequences when True.
        strip_whitespace: Trim leading/trailing whitespace when True.
        max_length: Optional maximum length to truncate the output.

    Returns:
        Cleaned output string.
    """

    cleaned = output or ""
    if strip_ansi:
        cleaned = re.sub(r"\x1B\[[0-?]*[ -/]*[@-~]", "", cleaned)
    if strip_whitespace:
        cleaned = cleaned.strip()
    if max_length is not None and max_length >= 0:
        cleaned = cleaned[:max_length]
    return cleaned


def detect_format(output: str) -> str:
    """
    Best-effort auto-detection of output format.

    Returns:
        One of: "json", "xml", "csv", or "text".
    """

    if not output or not output.strip():
        return "text"
    snippet = output.strip()
    for name, pattern in FORMAT_PATTERNS.items():
        if re.match(pattern, snippet, flags=re.IGNORECASE | re.MULTILINE):
            return name
    return "text"


def parse_crawler_line(line: str) -> Optional[Dict[str, Any]]:
    """
    Parse common web crawler finding lines such as Gobuster/Feroxbuster output.

    Supports patterns like:
        "/admin (Status: 200) [Size: 1234]"
        "/images (Status: 301)"

    Returns a dictionary with path, status, and optional size when detected.
    """

    cleaned_line = clean_output(line, strip_ansi=True)
    if not cleaned_line or not cleaned_line.startswith("/"):
        return None

    path_part = cleaned_line.split()[0]
    status_match = re.search(r"Status:\s*(\d+)", cleaned_line, flags=re.IGNORECASE)
    size_match = re.search(r"Size:\s*(\d+)", cleaned_line, flags=re.IGNORECASE)

    parsed: Dict[str, Any] = {"path": path_part}
    if status_match:
        parsed["status"] = int(status_match.group(1))
    if size_match:
        parsed["size"] = int(size_match.group(1))
    return parsed


def safe_extract(data: Dict[str, Any], keys: List[str], default: Any = None) -> Any:
    """
    Safely extract nested dictionary values using a sequence of keys.

    Args:
        data: Dictionary to traverse.
        keys: Sequence of keys to follow.
        default: Value to return if any key is missing.

    Returns:
        Extracted value or the default when missing.
    """

    current: Any = data
    for key in keys:
        if isinstance(current, dict) and key in current:
            current = current[key]
        else:
            return default
    return current


def parse_json_output(
    json_text: str,
    *,
    line_by_line: bool = False,
    default_keys: Optional[List[str]] = None,
    extract_nested: bool = True,
) -> JsonParseResult:
    """
    Parse JSON output from web application tools.

    Supports:
        - Single JSON objects (common for most tools).
        - JSON arrays.
        - Line-delimited JSON objects (amass-style) when `line_by_line=True`.

    Examples:
        metadata = parse_json_output(stdout)
        metadata = parse_json_output(stdout, line_by_line=True)  # amass pattern

    Args:
        json_text: Raw JSON text.
        line_by_line: Parse each line as JSON when True.
        default_keys: Keys to lift into summary when present.
        extract_nested: Flatten nested objects into summary when True.

    Returns:
        JsonParseResult with keys: data, summary, error, raw_output.
    """

    cleaned_text = clean_output(json_text)
    result: JsonParseResult = {"data": [], "summary": {}, "error": None, "raw_output": cleaned_text}

    if not cleaned_text:
        return result

    try:
        if line_by_line:
            parsed: List[Any] = []
            errors: List[str] = []
            for line in cleaned_text.splitlines():
                if not line.strip():
                    continue
                try:
                    parsed.append(json.loads(line))
                except json.JSONDecodeError as exc:  # pragma: no cover - partial parse logging
                    msg = f"Line JSON decode failed at pos {exc.pos}: {exc.msg}"
                    errors.append(msg)
                    logger.warning(msg)
            result["data"] = parsed
            if errors:
                result["error"] = "; ".join(errors)
        else:
            parsed_json = json.loads(cleaned_text)
            if isinstance(parsed_json, list):
                result["data"] = parsed_json
            else:
                result["data"] = [parsed_json]
    except json.JSONDecodeError as exc:
        result["error"] = f"JSON decode error at pos {exc.pos}: {exc.msg}"
        logger.warning(result["error"])
        return result

    summary: Dict[str, Any] = {}
    if default_keys:
        for key in default_keys:
            value = None
            for item in result["data"]:
                if isinstance(item, dict) and key in item:
                    value = item[key]
                    break
            if value is not None:
                summary[key] = value

    if extract_nested:
        for item in result["data"]:
            if isinstance(item, dict):
                for key, value in item.items():
                    if key not in summary:
                        summary[key] = value
    result["summary"] = summary
    return result


def parse_xml_output(
    xml_text: str,
    *,
    root_element: Optional[str] = None,
    extract_attributes: bool = True,
    extract_text: bool = True,
) -> XmlParseResult:
    """
    Parse XML output with a structure similar to nmap XML reports.

    Args:
        xml_text: Raw XML text.
        root_element: Optional root element name to filter on.
        extract_attributes: Include element attributes when True.
        extract_text: Include element text content when True.

    Returns:
        XmlParseResult with keys: elements, attributes, error, raw_output.
    """

    cleaned_text = clean_output(xml_text)
    result: XmlParseResult = {"elements": [], "attributes": {}, "error": None, "raw_output": cleaned_text}

    if not cleaned_text:
        return result

    try:
        root = ET.fromstring(cleaned_text)
    except ET.ParseError as exc:
        result["error"] = f"XML parse error: {exc}"
        logger.warning(result["error"])
        return result

    if root_element and root.tag != root_element:
        result["error"] = f"Unexpected root element: {root.tag}"
        return result

    def serialize_element(elem: ET.Element) -> Dict[str, Any]:
        serialized: Dict[str, Any] = {"tag": elem.tag}
        if extract_attributes and elem.attrib:
            serialized["attributes"] = dict(elem.attrib)
        if extract_text and (elem.text and elem.text.strip()):
            serialized["text"] = elem.text.strip()
        children = [serialize_element(child) for child in elem]
        if children:
            serialized["children"] = children
        return serialized

    result["elements"] = [serialize_element(root)]
    if extract_attributes:
        result["attributes"] = dict(root.attrib)
    return result


def parse_csv_output(
    csv_text: str,
    *,
    delimiter: str = ",",
    has_header: bool = True,
    skip_empty_rows: bool = True,
) -> CsvParseResult:
    """
    Parse CSV output produced by tools (e.g., ffuf or gobuster exports).

    Args:
        csv_text: Raw CSV text.
        delimiter: Field delimiter (default comma).
        has_header: Treat first row as header when True.
        skip_empty_rows: Ignore blank rows when True.

    Returns:
        CsvParseResult with keys: rows, headers, row_count, error, raw_output.
    """

    cleaned_text = clean_output(csv_text, strip_ansi=True, strip_whitespace=False)
    result: CsvParseResult = {"rows": [], "headers": [], "row_count": 0, "error": None, "raw_output": cleaned_text}

    if not cleaned_text.strip():
        return result

    try:
        reader_stream = StringIO(cleaned_text)
        if has_header:
            dict_reader = csv.DictReader(reader_stream, delimiter=delimiter)
            result["headers"] = dict_reader.fieldnames or []
            for row in dict_reader:
                if skip_empty_rows and not any(value for value in row.values()):
                    continue
                result["rows"].append(row)
        else:
            row_reader = csv.reader(reader_stream, delimiter=delimiter)
            for row in row_reader:
                if skip_empty_rows and not any(cell for cell in row):
                    continue
                result["rows"].append({"columns": row})
        result["row_count"] = len(result["rows"])
    except Exception as exc:  # pragma: no cover - defensive catch mirrors CLI usage
        result["error"] = f"CSV parse error: {exc}"
        logger.warning(result["error"])
    return result


def normalize_severity(
    severity: Union[str, int, float],
    *,
    output_format: str = "standard",
) -> str:
    """
    Normalize severity values into a standard level.

    Mapping rules (case-insensitive):
        - Strings: critical -> Critical, high -> High, medium -> Medium,
          low -> Low, info/informational -> Info.
        - CVSS (0-10): >=9 Critical, >=7 High, >=4 Medium, >0 Low, 0 Info.
        - Risk levels (1-5): 5/4 -> High, 3 -> Medium, 2/1 -> Low, 0 Info.
        - Tool-specific: common Nikto/SQLMap/OSVDB numeric severities align
          with the risk-level mapping; CVSS-like scores align with CVSS mapping.

    Args:
        severity: Severity value as string or numeric.
        output_format: "standard" (default), "cvss", or "simple".

    Returns:
        Normalized severity string or "Unknown".
    """

    if severity is None:
        return "Unknown"

    def to_standard(name: str) -> str:
        normalized = name.strip().lower()
        if normalized in {"critical"}:
            return SEVERITY_CRITICAL
        if normalized in {"high", "h"}:
            return SEVERITY_HIGH
        if normalized in {"medium", "med", "m"}:
            return SEVERITY_MEDIUM
        if normalized in {"low", "l"}:
            return SEVERITY_LOW
        if normalized in {"info", "informational", "information"}:
            return SEVERITY_INFO
        return "Unknown"

    standard_value = "Unknown"
    if isinstance(severity, str):
        match = re.search(r"(\d+(?:\.\d+)?)", severity)
        if match:
            try:
                numeric_value = float(match.group(1))
                standard_value = _numeric_to_standard(numeric_value)
            except ValueError:
                standard_value = to_standard(severity)
        else:
            standard_value = to_standard(severity)
    elif isinstance(severity, (int, float)):
        standard_value = _numeric_to_standard(float(severity))

    if output_format == "cvss":
        return _standard_to_cvss(standard_value)
    if output_format == "simple":
        return _standard_to_simple(standard_value)
    return standard_value


def _numeric_to_standard(value: float) -> str:
    # Risk-level mapping commonly used by Nikto/SQLMap (1-5 scale)
    if float(value).is_integer() and 0 <= value <= 5:
        int_value = int(value)
        if int_value in {5, 4}:
            return SEVERITY_HIGH
        if int_value == 3:
            return SEVERITY_MEDIUM
        if int_value in {2, 1}:
            return SEVERITY_LOW
        return SEVERITY_INFO

    # CVSS-like mapping (0-10 scale)
    if value >= 9.0:
        return SEVERITY_CRITICAL
    if value >= 7.0:
        return SEVERITY_HIGH
    if value >= 4.0:
        return SEVERITY_MEDIUM
    if value > 0:
        return SEVERITY_LOW
    return SEVERITY_INFO


def _standard_to_cvss(severity: str) -> str:
    mapping = {
        SEVERITY_CRITICAL: "9.0",
        SEVERITY_HIGH: "7.0",
        SEVERITY_MEDIUM: "5.0",
        SEVERITY_LOW: "2.5",
        SEVERITY_INFO: "0.0",
    }
    return mapping.get(severity, "0.0")


def _standard_to_simple(severity: str) -> str:
    mapping = {
        SEVERITY_CRITICAL: "High",
        SEVERITY_HIGH: "High",
        SEVERITY_MEDIUM: "Medium",
        SEVERITY_LOW: "Low",
        SEVERITY_INFO: "Low",
    }
    return mapping.get(severity, "Low")


def extract_vulnerabilities(
    data: Union[Dict[str, Any], List[Dict[str, Any]]],
    *,
    vuln_keys: Optional[List[str]] = None,
    severity_key: str = "severity",
    normalize_severity: bool = True,
) -> List[VulnerabilityEntry]:
    """
    Extract vulnerability entries from parsed tool data.

    Supports patterns observed in sqlmap/nikto/nuclei style outputs, allowing
    nested structures and configurable vulnerability keys.

    Args:
        data: Parsed data (dict or list of dicts).
        vuln_keys: Additional keys that may contain vulnerability lists.
        severity_key: Name of the severity field to read.
        normalize_severity: Normalize severity values when True.

    Returns:
        List of standardized vulnerability entries.
    """

    candidate_keys = VULN_KEYS + (vuln_keys or [])
    items: List[Any] = []
    severity_normalizer = globals().get("normalize_severity")

    if isinstance(data, dict):
        items.append(data)
    elif isinstance(data, list):
        items.extend(data)
    else:
        return []

    def extract_from_entry(entry: Dict[str, Any]) -> List[Dict[str, Any]]:
        aggregated: List[Dict[str, Any]] = []
        for key in candidate_keys:
            if key in entry and isinstance(entry[key], list):
                aggregated.extend(entry[key])
        return aggregated or [entry]

    vulnerabilities: List[VulnerabilityEntry] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        for raw_entry in extract_from_entry(item):
            if not isinstance(raw_entry, dict):
                continue
            severity_value = raw_entry.get(severity_key, raw_entry.get("risk"))
            normalized_severity = (
            severity_normalizer(severity_value) if normalize_severity and callable(severity_normalizer) else severity_value
            ) or "Unknown"
            vulnerabilities.append(
                VulnerabilityEntry(
                    type=raw_entry.get("type") or raw_entry.get("name"),
                    severity=normalized_severity,
                    description=raw_entry.get("description") or raw_entry.get("detail"),
                    parameter=raw_entry.get("parameter") or raw_entry.get("param"),
                    payload=raw_entry.get("payload"),
                    location=raw_entry.get("url") or raw_entry.get("path"),
                    raw=raw_entry,
                )
            )
    return vulnerabilities

