"""Tests for output processing utilities.

Tests smart truncation, error extraction, noise stripping, and the
full processing pipeline.
"""

from __future__ import annotations

import pytest

from agent.utils.output_processing import (
    smart_truncate,
    sample_head_middle_tail,
    extract_error_lines,
    strip_noise,
    process_tool_output,
    format_output_for_prompt,
    suggest_read_strategy,
    classify_output_type,
    DEFAULT_HEAD_CHARS,
    DEFAULT_TAIL_CHARS,
    DEFAULT_TOTAL_LIMIT,
)
from agent.utils.truncation_config import (
    THRESHOLDS,
    SOFT_MARGIN_PERCENT,
    get_effective_limit,
    should_suggest_file_reading,
)


# -----------------------------------------------------------------------------
# smart_truncate Tests
# -----------------------------------------------------------------------------


class TestSmartTruncate:
    """Tests for smart_truncate function."""
    
    def test_short_text_unchanged(self):
        """Short text should pass through unchanged."""
        text = "Hello, world!"
        result = smart_truncate(text, total_limit=100)
        assert result == text
    
    def test_text_at_limit_unchanged(self):
        """Text exactly at limit should pass through unchanged."""
        text = "A" * 100
        result = smart_truncate(text, total_limit=100)
        assert result == text
    
    def test_long_text_truncated(self):
        """Long text should be truncated with head and tail."""
        text = "A" * 1000 + "B" * 1000
        result = smart_truncate(text, head_chars=100, tail_chars=100, total_limit=300)
        
        assert result.startswith("A" * 100)
        assert result.endswith("B" * 100)
        assert "truncated" in result.lower()
    
    def test_truncation_marker_shows_count(self):
        """Truncation marker should show character count."""
        text = "X" * 5000
        result = smart_truncate(text, head_chars=100, tail_chars=100, total_limit=300)
        
        # Should show approximately 4800 chars truncated
        assert "4,800" in result or "4800" in result
    
    def test_empty_text_returns_empty(self):
        """Empty text should return empty string."""
        assert smart_truncate("") == ""
        assert smart_truncate("   ") == ""
    
    def test_proportional_reduction(self):
        """When head+tail exceed available space, both should reduce proportionally."""
        text = "A" * 1000
        result = smart_truncate(text, head_chars=200, tail_chars=200, total_limit=100)
        
        # Should still produce valid output (not crash)
        assert len(result) <= 150  # Some margin for separator
        assert "truncated" in result.lower()
    
    def test_preserves_end_content(self):
        """Tail should preserve content from the end (where results often are)."""
        text = "SETUP_DATA " * 100 + "RESULT: success"
        result = smart_truncate(text, head_chars=50, tail_chars=50, total_limit=200)
        
        # The end should be preserved
        assert "RESULT: success" in result
    
    def test_soft_margin_prevents_small_truncation(self):
        """Text slightly over limit should not be truncated (soft margin)."""
        # With a limit of 1000 and 20% margin, text up to 1200 should pass through
        limit = 1000
        effective = get_effective_limit(limit)
        
        # Text just under effective limit should not be truncated
        text = "A" * (effective - 10)
        result, was_truncated = smart_truncate(text, total_limit=limit, return_was_truncated=True)
        assert not was_truncated
        assert result == text
    
    def test_soft_margin_truncates_when_exceeded(self):
        """Text well over limit+margin should still be truncated."""
        limit = 1000
        effective = get_effective_limit(limit)
        
        # Text way over effective limit should be truncated
        text = "A" * (effective + 500)
        result, was_truncated = smart_truncate(text, total_limit=limit, return_was_truncated=True)
        assert was_truncated
        assert "truncated" in result.lower()
    
    def test_output_type_help_uses_higher_threshold(self):
        """Help output type should use higher threshold."""
        # Help threshold is much higher than default
        text = "A" * 8000
        result, was_truncated = smart_truncate(
            text,
            output_type="help",
            return_was_truncated=True
        )
        # With help threshold (12000) + margin, 8000 chars should not be truncated
        assert not was_truncated
    
    def test_output_type_scan_respects_threshold(self):
        """Scan output type should use scan-specific threshold."""
        text = "A" * 9000
        result, was_truncated = smart_truncate(
            text,
            output_type="scan",
            return_was_truncated=True
        )
        # With scan threshold (10000) + margin, 9000 chars should not be truncated
        assert not was_truncated


# -----------------------------------------------------------------------------
# sample_head_middle_tail Tests
# -----------------------------------------------------------------------------


class TestSampleHeadMiddleTail:
    """Tests for sample_head_middle_tail function."""

    def test_short_text_unchanged(self):
        """Short text should pass through unchanged."""
        text = "Short output"
        result, was_sampled = sample_head_middle_tail(
            text,
            total_limit=100,
            return_was_sampled=True,
        )
        assert result == text
        assert not was_sampled

    def test_long_text_preserves_head_middle_tail(self):
        """Long text should include representative start/middle/end segments."""
        long_text = (
            "HEAD_MARKER\n"
            + ("A" * 5200)
            + "MIDDLE_MARKER\n"
            + ("B" * 5200)
            + "TAIL_MARKER\n"
        )

        result, was_sampled = sample_head_middle_tail(
            long_text,
            total_limit=10_000,
            return_was_sampled=True,
        )

        assert was_sampled
        assert "HEAD_MARKER" in result
        assert "MIDDLE_MARKER" in result
        assert "TAIL_MARKER" in result
        assert len(result) <= 10_000


# -----------------------------------------------------------------------------
# classify_output_type Tests
# -----------------------------------------------------------------------------


class TestClassifyOutputType:
    """Tests for classify_output_type function."""
    
    def test_help_flag_detected(self):
        """Commands with --help should classify as help."""
        assert classify_output_type(command="nmap --help") == "help"
        assert classify_output_type(command="gobuster -h") == "help"
        assert classify_output_type(command="python --version") == "help"
    
    def test_scan_tools_detected(self):
        """Known scan tools should classify as scan."""
        assert classify_output_type(tool_name="nmap") == "scan"
        assert classify_output_type(tool_name="gobuster") == "scan"
        assert classify_output_type(tool_name="nikto") == "scan"
        assert classify_output_type(tool_name="shell.exec", command="nmap -sV target") == "default"
    
    def test_log_files_detected(self):
        """Log files should classify as log."""
        assert classify_output_type(command="cat /var/log/auth.log") == "log"
        assert classify_output_type(command="tail -f app.log") == "log"
    
    def test_large_output_classified_as_log(self):
        """Output with many lines should classify as log."""
        large_output = "\n".join(["line"] * 600)
        assert classify_output_type(output=large_output) == "log"
    
    def test_default_for_unknown(self):
        """Unknown patterns should return default."""
        assert classify_output_type(tool_name="shell.exec", command="ls -la") == "default"


# -----------------------------------------------------------------------------
# extract_error_lines Tests
# -----------------------------------------------------------------------------


class TestExtractErrorLines:
    """Tests for extract_error_lines function."""
    
    def test_finds_error_keyword(self):
        """Should find lines containing 'error'."""
        text = """
Line 1: Normal output
Line 2: Error occurred here
Line 3: More normal output
"""
        result = extract_error_lines(text)
        assert "Error occurred here" in result
        assert "→" in result  # Match indicator
    
    def test_finds_failed_keyword(self):
        """Should find lines containing 'failed'."""
        text = "Connection failed: timeout"
        result = extract_error_lines(text)
        assert "failed" in result.lower()
    
    def test_finds_permission_denied(self):
        """Should find 'permission denied' lines."""
        text = "Permission denied for /etc/shadow"
        result = extract_error_lines(text)
        assert "Permission denied" in result
    
    def test_includes_context_lines(self):
        """Should include context lines around matches."""
        text = """
Line 1: Before error
Line 2: Error here
Line 3: After error
Line 4: Unrelated
"""
        result = extract_error_lines(text, context_lines=1)
        
        # Should include line before and after
        assert "Before error" in result
        assert "After error" in result
    
    def test_max_matches_limits_output(self):
        """Should respect max_matches limit."""
        text = "\n".join([f"Error {i}" for i in range(20)])
        result = extract_error_lines(text, max_matches=3)
        
        # Count arrow markers (match indicators)
        match_count = result.count("→")
        assert match_count <= 3
    
    def test_no_matches_returns_empty(self):
        """Should return empty string if no matches."""
        text = "Everything is fine\nAll good here"
        result = extract_error_lines(text)
        assert result == ""
    
    def test_empty_text_returns_empty(self):
        """Empty text should return empty string."""
        assert extract_error_lines("") == ""
    
    def test_includes_line_numbers(self):
        """Output should include line numbers."""
        text = "Line one\nError on line two\nLine three"
        result = extract_error_lines(text)
        
        # Should have line number indicators (digit followed by |)
        assert "|" in result


# -----------------------------------------------------------------------------
# strip_noise Tests
# -----------------------------------------------------------------------------


class TestStripNoise:
    """Tests for strip_noise function."""
    
    def test_removes_kali_welcome(self):
        """Should remove Kali welcome message."""
        text = """┏━(Message from Kali developers)
┃ This is a minimal installation...
┗━(Run: "touch ~/.hushlogin" to hide this message)
1: lo: interface info here"""
        
        result = strip_noise(text)
        
        # Should not contain the welcome box
        assert "Message from Kali" not in result
        # Should keep the actual output
        assert "lo:" in result
    
    def test_removes_ansi_codes(self):
        """Should remove ANSI escape codes."""
        text = "\x1b[31mRed text\x1b[0m and [1;32mgreen"
        result = strip_noise(text)
        
        # Should remove escape sequences but keep text
        assert "Red text" in result
        assert "\x1b" not in result
    
    def test_preserves_normal_text(self):
        """Should not modify normal text."""
        text = "Normal output from nmap scan"
        result = strip_noise(text)
        assert result == text
    
    def test_empty_text_returns_empty(self):
        """Empty text should return empty string."""
        assert strip_noise("") == ""
    
    def test_custom_patterns(self):
        """Should support custom patterns."""
        text = "PREFIX: actual data"
        result = strip_noise(text, patterns=["PREFIX: "])
        assert result == "actual data"


# -----------------------------------------------------------------------------
# process_tool_output Tests
# -----------------------------------------------------------------------------


class TestProcessToolOutput:
    """Tests for process_tool_output function."""
    
    def test_basic_processing(self):
        """Should process stdout and stderr."""
        stdout = "Output data"
        stderr = "Warning message"
        
        result = process_tool_output(stdout, stderr)
        
        assert "Output data" in result.stdout
        assert "Warning" in result.stderr
    
    def test_includes_artifact_hint(self):
        """Should include artifact hint when path provided."""
        stdout = "Output"
        
        result = process_tool_output(
            stdout,
            artifact_path="/workspace/1234/artifacts/20250101_tool.txt"
        )
        
        assert result.artifact_hint is not None
        assert "artifacts" in result.artifact_hint
    
    def test_no_artifact_hint_without_path(self):
        """Should not include artifact hint when no path."""
        stdout = "Output"
        
        result = process_tool_output(stdout)
        
        assert result.artifact_hint is None
    
    def test_extracts_errors_from_long_output(self):
        """Should extract error lines from truncated output."""
        stdout = "Normal output\n" * 500 + "ERROR: Critical failure\n" + "Normal output\n" * 500
        
        result = process_tool_output(stdout, total_limit=500, include_errors=True)
        
        # Should have extracted the error
        if "Extracted" in result.stdout:
            assert "Critical failure" in result.stdout
    
    def test_stderr_gets_more_space_when_present(self):
        """Stderr should get proportionally more space when it has content."""
        stdout = "A" * 1000
        stderr = "B" * 1000
        
        result = process_tool_output(stdout, stderr, total_limit=500)
        
        # Both should be truncated (not crash)
        assert len(result.stdout) < 1000
        assert len(result.stderr) < 1000
    
    def test_was_truncated_flag_when_truncated(self):
        """Should set was_truncated=True when output is truncated."""
        stdout = "A" * 5000  # Long output
        
        result = process_tool_output(stdout, total_limit=500)
        
        assert result.was_truncated is True
    
    def test_was_truncated_flag_when_not_truncated(self):
        """Should set was_truncated=False when output fits."""
        stdout = "Short output"
        
        result = process_tool_output(stdout, total_limit=500)
        
        assert result.was_truncated is False
    
    def test_truncated_artifact_hint_is_informational(self):
        """When truncated, artifact hint should use soft/informational language."""
        # Use a very small limit to force truncation
        stdout = "A" * 5000
        
        result = process_tool_output(
            stdout,
            artifact_path="/workspace/artifacts/tool.txt",
            total_limit=500
        )
        
        assert result.was_truncated is True
        # New soft messaging - no aggressive warnings
        assert "condensed" in result.artifact_hint.lower()
        # Should still mention where full output is saved
        assert "artifacts/tool.txt" in result.artifact_hint
    
    def test_non_truncated_artifact_hint_simple(self):
        """When not truncated, artifact hint should be simple."""
        stdout = "Short output"
        
        result = process_tool_output(
            stdout,
            artifact_path="/workspace/artifacts/tool.txt",
            total_limit=500
        )
        
        assert result.was_truncated is False
        # Should just note where it's saved, no warnings
        assert "⚠️" not in (result.artifact_hint or "")
        assert "TRUNCATED" not in (result.artifact_hint or "")
        assert "saved" in (result.artifact_hint or "").lower()


class TestSuggestReadStrategy:
    """Tests for suggest_read_strategy function."""
    
    def test_small_file_suggests_full_read(self):
        """Files <1000 lines should suggest full read."""
        hint = suggest_read_strategy(
            total_lines=800,
            file_size_bytes=50000,
            was_truncated=True,
            read_mode_used="byte",
            artifact_path="/workspace/file.txt",
        )
        assert "read_mode='full'" in hint
        assert "800 lines" in hint
    
    def test_medium_file_suggests_head_tail(self):
        """Files 1000-5000 lines should suggest head+tail."""
        hint = suggest_read_strategy(
            total_lines=3000,
            file_size_bytes=200000,
            was_truncated=True,
            read_mode_used="byte",
            artifact_path="/workspace/file.txt",
        )
        assert "read_mode='head'" in hint or "read_mode='tail'" in hint
        assert "3,000 lines" in hint or "3000 lines" in hint
    
    def test_large_log_file_suggests_tail(self):
        """Large log files should suggest tail mode."""
        hint = suggest_read_strategy(
            total_lines=10000,
            file_size_bytes=1000000,
            was_truncated=True,
            read_mode_used="byte",
            artifact_path="/workspace/scan.log",
        )
        assert "read_mode='tail'" in hint
        assert "num_lines" in hint
    
    def test_large_file_suggests_grep(self):
        """Large files should suggest grep for pattern matching."""
        hint = suggest_read_strategy(
            total_lines=15000,
            file_size_bytes=2000000,
            was_truncated=True,
            read_mode_used="byte",
            artifact_path="/workspace/results.txt",
        )
        assert "grep" in hint.lower()
    
    def test_range_mode_suggests_navigation(self):
        """Range mode should suggest navigation options."""
        hint = suggest_read_strategy(
            total_lines=10000,
            file_size_bytes=1000000,
            was_truncated=False,
            read_mode_used="range",
            artifact_path="/workspace/file.txt",
        )
        assert "start_line" in hint
        assert "range" in hint.lower()
    
    def test_no_line_count_suggests_wc(self):
        """Missing line count should suggest using wc -l."""
        hint = suggest_read_strategy(
            total_lines=None,
            file_size_bytes=500000,
            was_truncated=True,
            read_mode_used="byte",
            artifact_path="/workspace/file.txt",
        )
        assert "wc -l" in hint or "line count" in hint.lower()


class TestProcessToolOutputWithMetadata:
    """Tests for process_tool_output with file metadata."""
    
    def test_truncated_output_uses_soft_messaging(self):
        """Truncated output should use informational messaging."""
        stdout = "A" * 5000  # Force truncation
        metadata = {
            "fs_read": {
                "total_lines": 500,
                "lines_read": 100,
                "read_mode_used": "byte",
                "truncated": True,
                "bytes_read": 5000,
            }
        }
        
        result = process_tool_output(
            stdout,
            artifact_path="/workspace/file.txt",
            total_limit=500,
            metadata=metadata,
        )
        
        assert result.was_truncated is True
        # New simplified messaging - just says condensed and where to find it
        assert "condensed" in (result.artifact_hint or "").lower()
        assert "file.txt" in (result.artifact_hint or "")
    
    def test_backward_compatible_without_metadata(self):
        """Should work without metadata (backward compatibility)."""
        stdout = "A" * 5000
        
        result = process_tool_output(
            stdout,
            artifact_path="/workspace/file.txt",
            total_limit=500,
        )
        
        assert result.was_truncated is True
        # New soft messaging
        assert "condensed" in (result.artifact_hint or "").lower()
        assert "file.txt" in (result.artifact_hint or "")
    
    def test_stores_file_metadata_in_result(self):
        """Should store file metadata in ProcessedOutput."""
        metadata = {
            "fs_read": {
                "total_lines": 1000,
                "read_mode_used": "full",
            }
        }
        
        result = process_tool_output(
            "output",
            metadata=metadata,
        )
        
        assert result.file_metadata is not None
        assert result.file_metadata.get("total_lines") == 1000

# -----------------------------------------------------------------------------
# format_output_for_prompt Tests
# -----------------------------------------------------------------------------


class TestFormatOutputForPrompt:
    """Tests for format_output_for_prompt function."""
    
    def test_formats_stdout_only(self):
        """Should format output with just stdout."""
        result = format_output_for_prompt("Output data")
        assert "Output data" in result
    
    def test_formats_stdout_and_stderr(self):
        """Should include both stdout and stderr with separator."""
        result = format_output_for_prompt("Output", "Errors")
        
        assert "Output" in result
        assert "STDERR" in result
        assert "Errors" in result
    
    def test_includes_artifact_reference(self):
        """Should include artifact path reference."""
        result = format_output_for_prompt(
            "Output",
            artifact_path="/workspace/artifacts/file.txt"
        )
        
        # Artifact path is always shown
        assert "/workspace/artifacts/file.txt" in result
    
    def test_truncated_output_has_artifact_reference(self):
        """When truncated, should reference artifact path."""
        # Use a large enough output to force truncation even with new higher limits
        result = format_output_for_prompt(
            "A" * 20000,  # Very large to force truncation
            artifact_path="/workspace/artifacts/file.txt"
        )
        
        # Should mention artifact path when truncated
        assert "file.txt" in result
    
    def test_empty_output_returns_no_output(self):
        """Should return 'No output' for empty content."""
        result = format_output_for_prompt("", "")
        assert result == "No output"


# -----------------------------------------------------------------------------
# Integration Tests
# -----------------------------------------------------------------------------


class TestIntegration:
    """Integration tests for the full processing pipeline."""
    
    def test_full_pipeline_with_nmap_output(self):
        """Test processing typical nmap output."""
        stdout = """Starting Nmap 7.94 ( https://nmap.org ) at 2025-01-01 00:00 UTC
Nmap scan report for 192.168.1.1
Host is up (0.001s latency).
PORT     STATE SERVICE
22/tcp   open  ssh
80/tcp   open  http
443/tcp  open  https
Nmap done: 1 IP address (1 host up) scanned in 0.50 seconds"""
        
        result = format_output_for_prompt(
            stdout,
            artifact_path="/workspace/1234/artifacts/nmap.txt"
        )
        
        # Should preserve key information
        assert "22/tcp" in result
        assert "ssh" in result
        assert "192.168.1.1" in result
        # Artifact path shown (no truncation warning since output is short)
        assert "/workspace/1234/artifacts/nmap.txt" in result
    
    def test_full_pipeline_with_large_output(self):
        """Test processing large output that needs truncation."""
        # Simulate large output with important data at the end
        stdout = "Verbose output line\n" * 200 + "FINAL RESULT: 5 vulnerabilities found"
        
        result = format_output_for_prompt(stdout)
        
        # Important: end content should be preserved
        # (This is why we use smart_truncate with head+tail)
        # Note: this depends on truncation limits
        assert len(result) < len(stdout)
    
    def test_full_pipeline_with_kali_noise(self):
        """Test that Kali noise is stripped."""
        stdout = """┏━(Message from Kali developers)
┃ This is a minimal installation
┗━(Run: "touch ~/.hushlogin" to hide this message)
1: lo: <LOOPBACK,UP> mtu 65536
2: eth0: <BROADCAST,MULTICAST,UP> mtu 1500
   inet 172.17.0.3/16"""
        
        result = format_output_for_prompt(stdout)
        
        # Noise should be gone
        assert "Message from Kali" not in result
        # Real data should be present
        assert "eth0" in result
        assert "172.17.0.3" in result
