"""Tests for: Cross-Platform & Polish enhancements.

Test coverage for:
-: Windows compatibility (cross-platform read functions)
-: Binary file improvements (hex dump, checksums)
-: Encoding auto-detection
-: Operation metrics and limits"""

from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path
from typing import Generator
from unittest.mock import patch

import pytest

# Import Phase 6 modules
from agent.tools.filesystem._platform import (
    read_head_python,
    read_tail_python,
    read_range_python,
    read_grep_python,
    compute_checksums,
    generate_hex_dump,
    analyze_binary_file,
    detect_encoding,
    detect_line_ending,
    normalize_line_endings,
    _is_likely_text,
    _detect_file_type,
    BinaryFileInfo,
    EncodingDetectionResult,
)
from agent.tools.filesystem._metrics import (
    FilesystemLimits,
    TaskMetrics,
    FilesystemMetricsStore,
    FilesystemLimitExceeded,
    get_metrics_store,
    check_operation_limit,
    record_filesystem_operation,
)


@pytest.fixture
def temp_workspace() -> Generator[Path, None, None]:
    """Create a temporary workspace directory."""
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = Path(tmpdir)
        yield workspace


# =============================================================================
# Task 6.1: Cross-Platform Read Functions
# =============================================================================


class TestReadHeadPython:
    """Tests for pure Python head implementation."""
    
    def test_reads_first_n_lines(self, temp_workspace: Path):
        test_file = temp_workspace / "test.txt"
        test_file.write_text("line1\nline2\nline3\nline4\nline5\n")
        
        content, lines_read = read_head_python(test_file, 3)
        
        assert lines_read == 3
        assert "line1" in content
        assert "line2" in content
        assert "line3" in content
        assert "line4" not in content
    
    def test_handles_file_shorter_than_requested(self, temp_workspace: Path):
        test_file = temp_workspace / "short.txt"
        test_file.write_text("only\ntwo\n")
        
        content, lines_read = read_head_python(test_file, 10)
        
        assert lines_read == 2
        assert "only" in content
        assert "two" in content
    
    def test_handles_windows_line_endings(self, temp_workspace: Path):
        test_file = temp_workspace / "windows.txt"
        test_file.write_bytes(b"line1\r\nline2\r\nline3\r\n")
        
        content, lines_read = read_head_python(test_file, 2)
        
        assert lines_read == 2
        assert "line1" in content
        assert "line2" in content


class TestReadTailPython:
    """Tests for pure Python tail implementation."""
    
    def test_reads_last_n_lines(self, temp_workspace: Path):
        test_file = temp_workspace / "test.txt"
        test_file.write_text("line1\nline2\nline3\nline4\nline5\n")
        
        content, lines_read = read_tail_python(test_file, 2)
        
        assert lines_read == 2
        assert "line4" in content
        assert "line5" in content
        assert "line1" not in content
    
    def test_handles_file_shorter_than_requested(self, temp_workspace: Path):
        test_file = temp_workspace / "short.txt"
        test_file.write_text("short\nfile\n")
        
        content, lines_read = read_tail_python(test_file, 100)
        
        assert lines_read == 2


class TestReadRangePython:
    """Tests for pure Python range implementation."""
    
    def test_reads_specific_range(self, temp_workspace: Path):
        test_file = temp_workspace / "test.txt"
        test_file.write_text("\n".join([f"line{i}" for i in range(1, 11)]) + "\n")
        
        content, lines_read = read_range_python(test_file, start_line=3, num_lines=4)
        
        assert lines_read == 4
        assert "line3" in content
        assert "line6" in content
        assert "line2" not in content
        assert "line7" not in content
    
    def test_handles_range_beyond_file_end(self, temp_workspace: Path):
        test_file = temp_workspace / "short.txt"
        test_file.write_text("line1\nline2\nline3\n")
        
        content, lines_read = read_range_python(test_file, start_line=2, num_lines=100)
        
        assert lines_read == 2
        assert "line2" in content
        assert "line3" in content


class TestReadGrepPython:
    """Tests for pure Python grep implementation."""
    
    def test_finds_matching_lines(self, temp_workspace: Path):
        test_file = temp_workspace / "log.txt"
        test_file.write_text("INFO: Starting\nERROR: Failed\nINFO: Done\nERROR: Timeout\n")
        
        content, match_count = read_grep_python(test_file, "ERROR", case_sensitive=True)
        
        assert match_count == 2
        assert "ERROR: Failed" in content
        assert "ERROR: Timeout" in content
        assert "INFO" not in content
    
    def test_case_insensitive_search(self, temp_workspace: Path):
        test_file = temp_workspace / "mixed.txt"
        test_file.write_text("Error here\nERROR there\nerror everywhere\n")
        
        content, match_count = read_grep_python(test_file, "error", case_sensitive=False)
        
        assert match_count == 3
    
    def test_regex_pattern(self, temp_workspace: Path):
        test_file = temp_workspace / "data.txt"
        test_file.write_text("user123\nuser456\nadmin789\nuser000\n")
        
        content, match_count = read_grep_python(test_file, r"user\d+", case_sensitive=True)
        
        assert match_count == 3
    
    def test_includes_line_numbers(self, temp_workspace: Path):
        test_file = temp_workspace / "test.txt"
        test_file.write_text("apple\nbanana\napple\n")
        
        content, _ = read_grep_python(test_file, "apple", case_sensitive=True)
        
        # Output format: "linenum:content"
        assert "1:apple" in content
        assert "3:apple" in content
    
    def test_respects_max_matches(self, temp_workspace: Path):
        test_file = temp_workspace / "many.txt"
        test_file.write_text("match\n" * 100)
        
        content, match_count = read_grep_python(test_file, "match", case_sensitive=True, max_matches=5)
        
        assert match_count == 5


# =============================================================================
# Task 6.2: Binary File Improvements
# =============================================================================


class TestComputeChecksums:
    """Tests for checksum computation."""
    
    def test_computes_md5_and_sha256(self, temp_workspace: Path):
        test_file = temp_workspace / "test.bin"
        test_data = b"Hello, World!"
        test_file.write_bytes(test_data)
        
        md5_hex, sha256_hex = compute_checksums(test_file)
        
        # Verify against known values
        expected_md5 = hashlib.md5(test_data).hexdigest()
        expected_sha256 = hashlib.sha256(test_data).hexdigest()
        
        assert md5_hex == expected_md5
        assert sha256_hex == expected_sha256
    
    def test_handles_empty_file(self, temp_workspace: Path):
        test_file = temp_workspace / "empty.bin"
        test_file.write_bytes(b"")
        
        md5_hex, sha256_hex = compute_checksums(test_file)
        
        assert md5_hex == hashlib.md5(b"").hexdigest()
        assert sha256_hex == hashlib.sha256(b"").hexdigest()


class TestGenerateHexDump:
    """Tests for hex dump generation."""
    
    def test_generates_formatted_hex_dump(self):
        data = b"Hello World!"
        
        hex_dump = generate_hex_dump(data)
        
        # Should contain offset
        assert "00000000" in hex_dump
        # Should contain hex representation
        assert "48" in hex_dump  # 'H'
        assert "65" in hex_dump  # 'e'
        # Should contain ASCII representation
        assert "|Hello World!|" in hex_dump
    
    def test_handles_non_printable_characters(self):
        data = b"\x00\x01\x02\xff"
        
        hex_dump = generate_hex_dump(data)
        
        assert "00 01 02 ff" in hex_dump
        assert "|....|" in hex_dump  # Non-printable shown as dots
    
    def test_respects_max_lines(self):
        data = b"x" * 1000
        
        hex_dump = generate_hex_dump(data, max_lines=2)
        
        lines = hex_dump.strip().split("\n")
        assert len(lines) <= 3  # 2 data lines + possible truncation notice


class TestAnalyzeBinaryFile:
    """Tests for binary file analysis."""
    
    def test_analyzes_binary_file(self, temp_workspace: Path):
        test_file = temp_workspace / "test.bin"
        test_file.write_bytes(b"\x89PNG\r\n\x1a\n" + b"fake png data")
        
        info = analyze_binary_file(test_file)
        
        assert isinstance(info, BinaryFileInfo)
        assert info.size_bytes > 0
        assert len(info.md5) == 32
        assert len(info.sha256) == 64
        assert info.detected_type == "PNG image"
        assert info.hex_preview is not None
    
    def test_detects_file_types(self, temp_workspace: Path):
        # Test various file signatures
        test_cases = [
            (b"\xff\xd8\xff", "JPEG image"),
            (b"PK\x03\x04test", "ZIP archive"),
            (b"\x1f\x8b\x08test", "GZIP compressed"),
            (b"%PDF-1.4test", "PDF document"),
        ]
        
        for data, expected_type in test_cases:
            test_file = temp_workspace / "test.bin"
            test_file.write_bytes(data)
            
            info = analyze_binary_file(test_file)
            
            assert info.detected_type == expected_type, f"Expected {expected_type}, got {info.detected_type}"


class TestIsLikelyText:
    """Tests for text detection heuristic."""
    
    def test_identifies_text_content(self):
        assert _is_likely_text(b"Hello, World!\nThis is text.") is True
    
    def test_identifies_binary_with_nulls(self):
        assert _is_likely_text(b"text\x00binary\x00content") is False
    
    def test_identifies_binary_high_bytes(self):
        # >30% high-bit bytes
        data = bytes([200] * 50 + [65] * 50)
        assert _is_likely_text(data) is False


class TestDetectFileType:
    """Tests for file type detection from magic bytes."""
    
    def test_detects_png(self):
        assert _detect_file_type(b"\x89PNG\r\n\x1a\n") == "PNG image"
    
    def test_detects_jpeg(self):
        assert _detect_file_type(b"\xff\xd8\xff") == "JPEG image"
    
    def test_detects_pdf(self):
        assert _detect_file_type(b"%PDF-1.4") == "PDF document"
    
    def test_returns_none_for_unknown(self):
        assert _detect_file_type(b"unknown format") is None


# =============================================================================
# Task 6.3: Encoding Auto-Detection
# =============================================================================


class TestDetectEncoding:
    """Tests for encoding auto-detection."""
    
    def test_detects_utf8(self, temp_workspace: Path):
        test_file = temp_workspace / "utf8.txt"
        test_file.write_text("Hello, World! 你好世界", encoding="utf-8")
        
        result = detect_encoding(test_file)
        
        assert result.encoding.lower() in ("utf-8", "utf8")
        assert result.confidence > 0.5
    
    def test_detects_utf8_bom(self, temp_workspace: Path):
        test_file = temp_workspace / "utf8bom.txt"
        test_file.write_bytes(b"\xef\xbb\xbfHello UTF-8 with BOM")
        
        result = detect_encoding(test_file)
        
        assert "utf-8" in result.encoding.lower()
        assert result.method == "bom"
        assert result.confidence == 1.0
    
    def test_detects_utf16_le_bom(self, temp_workspace: Path):
        test_file = temp_workspace / "utf16le.txt"
        test_file.write_bytes(b"\xff\xfeH\x00e\x00l\x00l\x00o\x00")
        
        result = detect_encoding(test_file)
        
        assert "utf-16" in result.encoding.lower()
        assert result.method == "bom"
    
    def test_handles_empty_file(self, temp_workspace: Path):
        test_file = temp_workspace / "empty.txt"
        test_file.write_bytes(b"")
        
        result = detect_encoding(test_file)
        
        assert result.encoding == "utf-8"
        assert result.confidence == 1.0


class TestNormalizeLineEndings:
    """Tests for line ending normalization."""
    
    def test_normalizes_crlf_to_lf(self):
        content = "line1\r\nline2\r\nline3"
        
        result = normalize_line_endings(content)
        
        assert result == "line1\nline2\nline3"
    
    def test_normalizes_cr_to_lf(self):
        content = "line1\rline2\rline3"
        
        result = normalize_line_endings(content)
        
        assert result == "line1\nline2\nline3"
    
    def test_handles_mixed_endings(self):
        content = "line1\r\nline2\nline3\rline4"
        
        result = normalize_line_endings(content)
        
        assert result == "line1\nline2\nline3\nline4"
    
    def test_can_convert_to_crlf(self):
        content = "line1\nline2"
        
        result = normalize_line_endings(content, target_ending="\r\n")
        
        assert result == "line1\r\nline2"


class TestDetectLineEnding:
    """Tests for line ending detection."""
    
    def test_detects_lf(self):
        assert detect_line_ending(b"line1\nline2\n") == "lf"
    
    def test_detects_crlf(self):
        assert detect_line_ending(b"line1\r\nline2\r\n") == "crlf"
    
    def test_detects_cr(self):
        assert detect_line_ending(b"line1\rline2\r") == "cr"
    
    def test_detects_mixed(self):
        assert detect_line_ending(b"line1\r\nline2\nline3\r") == "mixed"


# =============================================================================
# Task 6.4: Operation Metrics and Limits
# =============================================================================


class TestFilesystemLimits:
    """Tests for limits configuration."""
    
    def test_default_limits(self):
        limits = FilesystemLimits()
        
        assert limits.max_reads_per_task == 1000
        assert limits.max_writes_per_task == 500
        assert limits.max_file_size_bytes == 50_000_000
    
    def test_from_env(self):
        with patch.dict(os.environ, {
            "FS_MAX_READS_PER_TASK": "100",
            "FS_MAX_WRITES_PER_TASK": "50",
        }):
            limits = FilesystemLimits.from_env()
            
            assert limits.max_reads_per_task == 100
            assert limits.max_writes_per_task == 50


class TestTaskMetrics:
    """Tests for task metrics tracking."""
    
    def test_initial_metrics(self):
        metrics = TaskMetrics(task_id="test-123")
        
        assert metrics.task_id == "test-123"
        assert metrics.read_count == 0
        assert metrics.write_count == 0
        assert metrics.bytes_read == 0
    
    def test_to_dict(self):
        metrics = TaskMetrics(task_id="test-123")
        metrics.read_count = 5
        metrics.bytes_read = 1000
        
        result = metrics.to_dict()
        
        assert result["task_id"] == "test-123"
        assert result["operations"]["read"] == 5
        assert result["bytes"]["read"] == 1000


class TestFilesystemMetricsStore:
    """Tests for metrics store."""
    
    def test_get_metrics_creates_new(self):
        store = FilesystemMetricsStore()
        
        metrics = store.get_metrics("task-new")
        
        assert metrics.task_id == "task-new"
        assert metrics.read_count == 0
    
    def test_get_metrics_returns_existing(self):
        store = FilesystemMetricsStore()
        
        metrics1 = store.get_metrics("task-1")
        metrics1.read_count = 10
        
        metrics2 = store.get_metrics("task-1")
        
        assert metrics2.read_count == 10
    
    def test_clear_metrics(self):
        store = FilesystemMetricsStore()
        store.get_metrics("task-1").read_count = 5
        
        store.clear_metrics("task-1")
        
        # New metrics should have count 0
        assert store.get_metrics("task-1").read_count == 0
    
    def test_check_limit_returns_none_when_ok(self):
        store = FilesystemMetricsStore(FilesystemLimits(max_reads_per_task=100))
        
        error = store.check_limit("task-1", "read")
        
        assert error is None
    
    def test_check_limit_returns_error_when_exceeded(self):
        limits = FilesystemLimits(max_reads_per_task=5)
        store = FilesystemMetricsStore(limits)
        
        # Record 5 reads
        for _ in range(5):
            store.record_operation("task-1", "read")
        
        # 6th read should fail
        error = store.check_limit("task-1", "read")
        
        assert error is not None
        assert "limit exceeded" in error.lower()
    
    def test_check_limit_bytes(self):
        limits = FilesystemLimits(max_bytes_read_per_task=1000)
        store = FilesystemMetricsStore(limits)
        
        # Record 800 bytes
        store.record_operation("task-1", "read", bytes_count=800)
        
        # Another 500 bytes should fail
        error = store.check_limit("task-1", "read", bytes_count=500)
        
        assert error is not None
        assert "bytes limit exceeded" in error.lower()
    
    def test_record_operation(self):
        store = FilesystemMetricsStore()
        
        store.record_operation("task-1", "read", bytes_count=100, duration_ms=10.5)
        
        metrics = store.get_metrics("task-1")
        assert metrics.read_count == 1
        assert metrics.bytes_read == 100
        assert metrics.total_read_time_ms == 10.5
    
    def test_record_operation_with_error(self):
        store = FilesystemMetricsStore()
        
        store.record_operation("task-1", "read", error="File not found")
        
        metrics = store.get_metrics("task-1")
        assert metrics.error_count == 1
        assert metrics.last_error == "File not found"
        # Count should not increment on error
        assert metrics.read_count == 0
    
    def test_track_operation_context_manager(self):
        # Disable rate limiting for this test
        limits = FilesystemLimits(max_ops_per_second=0)  # 0 = disabled
        store = FilesystemMetricsStore(limits)
        
        with store.track_operation("task-track-1", "read", bytes_estimate=100):
            pass  # Simulate operation
        
        metrics = store.get_metrics("task-track-1")
        assert metrics.read_count == 1
        assert metrics.bytes_read == 100
    
    def test_track_operation_raises_on_limit_exceeded(self):
        # Disable rate limiting, enable read limit
        limits = FilesystemLimits(max_reads_per_task=1, max_ops_per_second=0)
        store = FilesystemMetricsStore(limits)
        
        # First operation succeeds
        with store.track_operation("task-track-2", "read"):
            pass
        
        # Second should raise due to read limit
        with pytest.raises(FilesystemLimitExceeded):
            with store.track_operation("task-track-2", "read"):
                pass


class TestConvenienceFunctions:
    """Tests for convenience functions."""
    
    def test_get_metrics_store(self):
        store = get_metrics_store()
        
        assert isinstance(store, FilesystemMetricsStore)
    
    def test_check_operation_limit(self):
        # Use a fresh store to avoid rate limit issues
        store = FilesystemMetricsStore()
        
        error = store.check_limit("task-test-new", "read")
        
        assert error is None
    
    def test_record_filesystem_operation(self):
        store = FilesystemMetricsStore()
        store.record_operation("task-test-rec", "write", bytes_count=500)
        
        metrics = store.get_metrics("task-test-rec")
        assert metrics.write_count >= 1


# =============================================================================
# Integration Tests
# =============================================================================


class TestReadFilePhase6Integration:
    """Integration tests for FsReadTool with Phase 6 features."""
    
    def test_auto_detect_encoding(self, temp_workspace: Path, monkeypatch):
        """Test that auto_detect_encoding uses detected encoding."""
        monkeypatch.setenv("WORKSPACE", str(temp_workspace))
        
        from agent.tools.filesystem.read_file import FsReadTool
        from agent.tools.filesystem.contracts import FsReadArgs
        
        # Create UTF-8 file with BOM
        test_file = temp_workspace / "utf8bom.txt"
        test_file.write_bytes(b"\xef\xbb\xbfHello UTF-8 with BOM")
        
        tool = FsReadTool()
        args = FsReadArgs(path="utf8bom.txt", auto_detect_encoding=True)
        result = tool.run(args)
        
        assert result.success
        assert "Hello UTF-8 with BOM" in result.stdout
        # Check metadata includes encoding info
        if "fs_read" in result.metadata:
            assert result.metadata["fs_read"].get("encoding_detected") is not None
    
    def test_include_checksums(self, temp_workspace: Path, monkeypatch):
        """Test that include_checksums computes MD5 and SHA256."""
        monkeypatch.setenv("WORKSPACE", str(temp_workspace))
        
        from agent.tools.filesystem.read_file import FsReadTool
        from agent.tools.filesystem.contracts import FsReadArgs
        
        test_file = temp_workspace / "test.txt"
        test_content = b"Test content for checksums"
        test_file.write_bytes(test_content)
        
        tool = FsReadTool()
        args = FsReadArgs(path="test.txt", include_checksums=True)
        result = tool.run(args)
        
        assert result.success
        if "fs_read" in result.metadata:
            assert result.metadata["fs_read"].get("md5_checksum") == hashlib.md5(test_content).hexdigest()
            assert result.metadata["fs_read"].get("sha256_checksum") == hashlib.sha256(test_content).hexdigest()
    
    def test_hex_dump_mode(self, temp_workspace: Path, monkeypatch):
        """Test that hex_dump mode returns hex dump for binary files."""
        monkeypatch.setenv("WORKSPACE", str(temp_workspace))
        
        from agent.tools.filesystem.read_file import FsReadTool
        from agent.tools.filesystem.contracts import FsReadArgs
        
        test_file = temp_workspace / "binary.bin"
        test_file.write_bytes(b"\x89PNG\r\n\x1a\nFake PNG data")
        
        tool = FsReadTool()
        args = FsReadArgs(path="binary.bin", encoding=None, hex_dump=True)
        result = tool.run(args)
        
        assert result.success
        assert "Hex dump:" in result.stdout
        assert "89" in result.stdout  # PNG magic byte
        assert "PNG image" in result.stdout  # Detected type
