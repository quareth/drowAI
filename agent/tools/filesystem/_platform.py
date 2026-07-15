"""Cross-platform filesystem utilities.

 -: Windows Compatibility
 -: Binary File Improvements (hex dump, checksums)
 -: Encoding Auto-Detection

This module provides pure Python implementations that work on both
Windows and Unix systems, replacing subprocess calls to Unix utilities."""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)


# =============================================================================
# Cross-platform read operations
# =============================================================================


def read_head_python(
    target: Path,
    num_lines: int,
    encoding: str = "utf-8",
) -> Tuple[str, int]:
    """Read first N lines using pure Python (cross-platform).
    
    Args:
        target: File path to read
        num_lines: Number of lines from the beginning
        encoding: Text encoding
        
    Returns:
        Tuple of (content, lines_read)
    """
    lines: List[str] = []
    try:
        with target.open("r", encoding=encoding, errors="replace", newline="") as f:
            for i, line in enumerate(f):
                if i >= num_lines:
                    break
                lines.append(line.rstrip("\r\n"))
    except OSError as e:
        raise OSError(f"Failed to read {target}: {e}") from e
    
    content = "\n".join(lines)
    return content, len(lines)


def read_tail_python(
    target: Path,
    num_lines: int,
    encoding: str = "utf-8",
) -> Tuple[str, int]:
    """Read last N lines using pure Python (cross-platform).
    
    Uses a deque-based approach for memory efficiency with large files.
    
    Args:
        target: File path to read
        num_lines: Number of lines from the end
        encoding: Text encoding
        
    Returns:
        Tuple of (content, lines_read)
    """
    from collections import deque
    
    try:
        with target.open("r", encoding=encoding, errors="replace", newline="") as f:
            # Use deque with maxlen for O(1) memory bounded tail
            tail_lines = deque(maxlen=num_lines)
            for line in f:
                tail_lines.append(line.rstrip("\r\n"))
    except OSError as e:
        raise OSError(f"Failed to read {target}: {e}") from e
    
    lines = list(tail_lines)
    content = "\n".join(lines)
    return content, len(lines)


def read_range_python(
    target: Path,
    start_line: int,
    num_lines: int,
    encoding: str = "utf-8",
) -> Tuple[str, int]:
    """Read a specific line range using pure Python (cross-platform).
    
    Args:
        target: File path to read
        start_line: First line to read (1-indexed)
        num_lines: Number of lines to read
        encoding: Text encoding
        
    Returns:
        Tuple of (content, lines_read)
    """
    lines: List[str] = []
    end_line = start_line + num_lines - 1
    
    try:
        with target.open("r", encoding=encoding, errors="replace", newline="") as f:
            for i, line in enumerate(f, start=1):
                if i < start_line:
                    continue
                if i > end_line:
                    break
                lines.append(line.rstrip("\r\n"))
    except OSError as e:
        raise OSError(f"Failed to read {target}: {e}") from e
    
    content = "\n".join(lines)
    return content, len(lines)


def read_grep_python(
    target: Path,
    pattern: str,
    case_sensitive: bool,
    encoding: str = "utf-8",
    max_matches: int = 200,
) -> Tuple[str, int]:
    """Search for pattern in file using pure Python (cross-platform).
    
    Args:
        target: File path to search
        pattern: Regex pattern to match
        case_sensitive: Whether to match case
        encoding: Text encoding
        max_matches: Maximum number of matching lines to return
        
    Returns:
        Tuple of (content with line numbers, match_count)
    """
    flags = 0 if case_sensitive else re.IGNORECASE
    
    try:
        regex = re.compile(pattern, flags)
    except re.error as e:
        raise ValueError(f"Invalid regex pattern '{pattern}': {e}") from e
    
    matches: List[str] = []
    try:
        with target.open("r", encoding=encoding, errors="replace", newline="") as f:
            for line_num, line in enumerate(f, start=1):
                if len(matches) >= max_matches:
                    break
                line_clean = line.rstrip("\r\n")
                if regex.search(line_clean):
                    matches.append(f"{line_num}:{line_clean}")
    except OSError as e:
        raise OSError(f"Failed to read {target}: {e}") from e
    
    content = "\n".join(matches)
    return content, len(matches)


# =============================================================================
# Binary file improvements
# =============================================================================


@dataclass
class BinaryFileInfo:
    """Information about a binary file."""
    size_bytes: int
    md5: str
    sha256: str
    is_text_likely: bool
    detected_type: Optional[str]
    hex_preview: str
    ascii_preview: str


def compute_checksums(target: Path) -> Tuple[str, str]:
    """Compute MD5 and SHA256 checksums of a file.
    
    Args:
        target: File path
        
    Returns:
        Tuple of (md5_hex, sha256_hex)
    """
    md5_hash = hashlib.md5()
    sha256_hash = hashlib.sha256()
    
    try:
        with target.open("rb") as f:
            # Read in chunks for memory efficiency
            for chunk in iter(lambda: f.read(65536), b""):
                md5_hash.update(chunk)
                sha256_hash.update(chunk)
    except OSError as e:
        raise OSError(f"Failed to read {target} for checksum: {e}") from e
    
    return md5_hash.hexdigest(), sha256_hash.hexdigest()


def generate_hex_dump(
    data: bytes,
    offset: int = 0,
    bytes_per_line: int = 16,
    max_lines: int = 32,
) -> str:
    """Generate a hex dump of binary data.
    
    Format: OFFSET  HEX_BYTES                                         ASCII
            00000000  48 65 6c 6c 6f 20 57 6f  72 6c 64 21 0a 00 00 00  |Hello World!....|
    
    Args:
        data: Binary data to dump
        offset: Starting offset for display
        bytes_per_line: Bytes per line (default 16)
        max_lines: Maximum lines to generate
        
    Returns:
        Formatted hex dump string
    """
    lines = []
    
    for i in range(0, min(len(data), bytes_per_line * max_lines), bytes_per_line):
        chunk = data[i:i + bytes_per_line]
        
        # Offset
        offset_str = f"{offset + i:08x}"
        
        # Hex bytes with mid-line gap
        hex_parts = []
        for j, byte in enumerate(chunk):
            if j == bytes_per_line // 2:
                hex_parts.append(" ")
            hex_parts.append(f"{byte:02x}")
        hex_str = " ".join(hex_parts)
        # Pad to fixed width
        hex_width = (bytes_per_line * 3) + 1  # 3 chars per byte + mid gap
        hex_str = hex_str.ljust(hex_width)
        
        # ASCII representation
        ascii_chars = []
        for byte in chunk:
            if 32 <= byte < 127:
                ascii_chars.append(chr(byte))
            else:
                ascii_chars.append(".")
        ascii_str = "".join(ascii_chars)
        
        lines.append(f"{offset_str}  {hex_str} |{ascii_str}|")
    
    if len(data) > bytes_per_line * max_lines:
        lines.append(f"... ({len(data) - bytes_per_line * max_lines} more bytes)")
    
    return "\n".join(lines)


def analyze_binary_file(
    target: Path,
    preview_bytes: int = 512,
    hex_lines: int = 32,
) -> BinaryFileInfo:
    """Analyze a binary file and return structured information.
    
    Args:
        target: File path
        preview_bytes: Number of bytes to read for preview/analysis
        hex_lines: Number of hex dump lines
        
    Returns:
        BinaryFileInfo with checksums, hex dump, and type detection
    """
    size_bytes = target.stat().st_size
    
    # Compute checksums
    md5_hex, sha256_hex = compute_checksums(target)
    
    # Read preview for analysis
    with target.open("rb") as f:
        preview_data = f.read(preview_bytes)
    
    # Detect if likely text
    is_text_likely = _is_likely_text(preview_data)
    
    # Detect file type from magic bytes
    detected_type = _detect_file_type(preview_data)
    
    # Generate hex dump
    hex_preview = generate_hex_dump(preview_data, max_lines=hex_lines)
    
    # Generate ASCII preview (printable chars only)
    ascii_chars = []
    for byte in preview_data[:256]:
        if 32 <= byte < 127:
            ascii_chars.append(chr(byte))
        elif byte in (10, 13, 9):  # newline, CR, tab
            ascii_chars.append(" ")
    ascii_preview = "".join(ascii_chars).strip()
    if len(ascii_preview) > 200:
        ascii_preview = ascii_preview[:200] + "..."
    
    return BinaryFileInfo(
        size_bytes=size_bytes,
        md5=md5_hex,
        sha256=sha256_hex,
        is_text_likely=is_text_likely,
        detected_type=detected_type,
        hex_preview=hex_preview,
        ascii_preview=ascii_preview,
    )


def _is_likely_text(data: bytes) -> bool:
    """Heuristically determine if data is likely text.
    
    Uses the same heuristic as Git: if >30% of first 8000 bytes
    are non-text bytes, it's probably binary.
    """
    if not data:
        return True
    
    # Check for null bytes (strong binary indicator)
    if b"\x00" in data[:8000]:
        return False
    
    # Count non-text bytes
    non_text = 0
    sample = data[:8000]
    for byte in sample:
        # Text bytes: printable ASCII, tab, newline, CR
        if not (32 <= byte < 127 or byte in (9, 10, 13)):
            non_text += 1
    
    return (non_text / len(sample)) < 0.30


def _detect_file_type(data: bytes) -> Optional[str]:
    """Detect file type from magic bytes.
    
    Returns a human-readable file type string or None if unknown.
    """
    if len(data) < 2:
        return None
    
    # Common magic byte signatures
    signatures = [
        (b"\x89PNG\r\n\x1a\n", "PNG image"),
        (b"\xff\xd8\xff\xe0", "JPEG image"),  # JFIF
        (b"\xff\xd8\xff\xe1", "JPEG image"),  # EXIF
        (b"\xff\xd8\xff\xdb", "JPEG image"),  # Raw JPEG
        (b"\xff\xd8\xff", "JPEG image"),  # Generic JPEG (3-byte prefix)
        (b"GIF87a", "GIF image"),
        (b"GIF89a", "GIF image"),
        (b"PK\x03\x04", "ZIP archive"),
        (b"PK\x05\x06", "ZIP archive (empty)"),
        (b"\x1f\x8b", "GZIP compressed"),
        (b"BZh", "BZIP2 compressed"),
        (b"\xfd7zXZ\x00", "XZ compressed"),
        (b"Rar!\x1a\x07", "RAR archive"),
        (b"\x7fELF", "ELF executable"),
        (b"MZ", "Windows executable (PE/DOS)"),
        (b"%PDF", "PDF document"),
        (b"<!DOCTYPE", "HTML document"),
        (b"<html", "HTML document"),
        (b"<?xml", "XML document"),
        (b"{\n", "JSON data (likely)"),
        (b"---", "YAML data (likely)"),
        (b"SQLite format", "SQLite database"),
    ]
    
    for magic, file_type in signatures:
        if data.startswith(magic):
            return file_type
    
    return None


# =============================================================================
# Encoding auto-detection
# =============================================================================


@dataclass
class EncodingDetectionResult:
    """Result of encoding detection."""
    encoding: str
    confidence: float
    method: str  # "bom", "chardet", "heuristic", "default"


def detect_encoding(
    target: Path,
    sample_size: int = 65536,
) -> EncodingDetectionResult:
    """Auto-detect file encoding.
    
    Detection priority:
    1. BOM (Byte Order Mark) - definitive
    2. chardet library if available - high confidence
    3. Heuristic analysis - medium confidence
    4. Default to UTF-8 - fallback
    
    Args:
        target: File path
        sample_size: Number of bytes to sample for detection
        
    Returns:
        EncodingDetectionResult with detected encoding and confidence
    """
    try:
        with target.open("rb") as f:
            data = f.read(sample_size)
    except OSError as e:
        logger.warning(f"Failed to read {target} for encoding detection: {e}")
        return EncodingDetectionResult("utf-8", 0.5, "default")
    
    if not data:
        return EncodingDetectionResult("utf-8", 1.0, "empty_file")
    
    # 1. Check for BOM
    bom_result = _detect_bom(data)
    if bom_result:
        return bom_result
    
    # 2. Try chardet if available
    chardet_result = _detect_with_chardet(data)
    if chardet_result:
        return chardet_result
    
    # 3. Heuristic detection
    return _detect_heuristic(data)


def _detect_bom(data: bytes) -> Optional[EncodingDetectionResult]:
    """Detect encoding from Byte Order Mark."""
    bom_encodings = [
        (b"\xef\xbb\xbf", "utf-8-sig"),
        (b"\xff\xfe\x00\x00", "utf-32-le"),
        (b"\x00\x00\xfe\xff", "utf-32-be"),
        (b"\xff\xfe", "utf-16-le"),
        (b"\xfe\xff", "utf-16-be"),
    ]
    
    for bom, encoding in bom_encodings:
        if data.startswith(bom):
            return EncodingDetectionResult(encoding, 1.0, "bom")
    
    return None


def _detect_with_chardet(data: bytes) -> Optional[EncodingDetectionResult]:
    """Detect encoding using chardet library if available."""
    try:
        import chardet
        result = chardet.detect(data)
        
        if result and result.get("encoding"):
            encoding = result["encoding"].lower()
            confidence = result.get("confidence", 0.0)
            
            # Normalize common encoding names
            encoding_map = {
                "ascii": "utf-8",  # ASCII is subset of UTF-8
                "iso-8859-1": "latin-1",
                "windows-1252": "cp1252",
            }
            encoding = encoding_map.get(encoding, encoding)
            
            return EncodingDetectionResult(encoding, confidence, "chardet")
    except ImportError:
        pass
    except Exception as e:
        logger.debug(f"chardet detection failed: {e}")
    
    return None


def _detect_heuristic(data: bytes) -> EncodingDetectionResult:
    """Heuristic encoding detection when chardet is unavailable."""
    # Try UTF-8 decode
    try:
        data.decode("utf-8")
        return EncodingDetectionResult("utf-8", 0.9, "heuristic")
    except UnicodeDecodeError:
        pass
    
    # Check for high-bit characters (suggests non-ASCII)
    has_high_bit = any(b >= 128 for b in data)
    
    if not has_high_bit:
        # Pure ASCII - use UTF-8
        return EncodingDetectionResult("utf-8", 0.95, "heuristic")
    
    # Try common encodings
    for encoding in ["latin-1", "cp1252", "iso-8859-1"]:
        try:
            data.decode(encoding)
            return EncodingDetectionResult(encoding, 0.6, "heuristic")
        except (UnicodeDecodeError, LookupError):
            pass
    
    # Fallback
    return EncodingDetectionResult("utf-8", 0.5, "default")


# =============================================================================
# Line ending normalization
# =============================================================================


def normalize_line_endings(content: str, target_ending: str = "\n") -> str:
    """Normalize line endings to a consistent format.
    
    Args:
        content: Text content with potentially mixed line endings
        target_ending: Target line ending (default: Unix LF)
        
    Returns:
        Content with normalized line endings
    """
    # First normalize all to LF
    content = content.replace("\r\n", "\n").replace("\r", "\n")
    
    # Then convert to target if not LF
    if target_ending != "\n":
        content = content.replace("\n", target_ending)
    
    return content


def detect_line_ending(data: bytes) -> str:
    """Detect the predominant line ending in binary data.
    
    Returns:
        "crlf" (Windows), "lf" (Unix), "cr" (old Mac), or "mixed"
    """
    crlf_count = data.count(b"\r\n")
    # Count standalone CR and LF (not part of CRLF)
    data_no_crlf = data.replace(b"\r\n", b"")
    cr_count = data_no_crlf.count(b"\r")
    lf_count = data_no_crlf.count(b"\n")
    
    total = crlf_count + cr_count + lf_count
    if total == 0:
        return "lf"  # Default for single-line files
    
    if crlf_count > 0 and cr_count == 0 and lf_count == 0:
        return "crlf"
    if lf_count > 0 and cr_count == 0 and crlf_count == 0:
        return "lf"
    if cr_count > 0 and lf_count == 0 and crlf_count == 0:
        return "cr"
    
    return "mixed"
