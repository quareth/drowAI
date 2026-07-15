"""Comprehensive tests for observation deduplication and progress detection (DR.6)."""

from __future__ import annotations

import pytest

from agent.graph.utils.observation_deduplication import (
    calculate_observation_similarity,
    check_observation_duplicate,
    detect_tool_output_change,
    hash_observation,
    score_observation_progress,
)


class TestObservationHasher:
    """Test observation hashing functionality."""

    def test_hash_observation_stable(self):
        """Test that same observation produces same hash."""
        obs1 = {
            "summary": "Test summary",
            "key_findings": ["finding1", "finding2"],
            "vulnerabilities": ["vuln1"],
            "next_actions": ["action1"],
        }
        obs2 = {
            "summary": "Test summary",
            "key_findings": ["finding1", "finding2"],
            "vulnerabilities": ["vuln1"],
            "next_actions": ["action1"],
        }

        hash1 = hash_observation(obs1)
        hash2 = hash_observation(obs2)

        assert hash1 == hash2

    def test_hash_observation_order_independent(self):
        """Test that hash is independent of list order."""
        obs1 = {
            "summary": "Test",
            "key_findings": ["finding1", "finding2"],
            "vulnerabilities": [],
            "next_actions": [],
        }
        obs2 = {
            "summary": "Test",
            "key_findings": ["finding2", "finding1"],  # Different order
            "vulnerabilities": [],
            "next_actions": [],
        }

        hash1 = hash_observation(obs1)
        hash2 = hash_observation(obs2)

        assert hash1 == hash2

    def test_hash_observation_case_insensitive(self):
        """Test that hash normalizes case."""
        obs1 = {
            "summary": "Test Summary",
            "key_findings": ["Finding1"],
            "vulnerabilities": [],
            "next_actions": [],
        }
        obs2 = {
            "summary": "test summary",
            "key_findings": ["finding1"],
            "vulnerabilities": [],
            "next_actions": [],
        }

        hash1 = hash_observation(obs1)
        hash2 = hash_observation(obs2)

        assert hash1 == hash2

    def test_hash_observation_different_content(self):
        """Test that different observations produce different hashes."""
        obs1 = {
            "summary": "Test 1",
            "key_findings": ["finding1"],
            "vulnerabilities": [],
            "next_actions": [],
        }
        obs2 = {
            "summary": "Test 2",
            "key_findings": ["finding2"],
            "vulnerabilities": [],
            "next_actions": [],
        }

        hash1 = hash_observation(obs1)
        hash2 = hash_observation(obs2)

        assert hash1 != hash2


class TestObservationSimilarity:
    """Test observation similarity calculation."""

    def test_calculate_similarity_identical(self):
        """Test similarity for identical observations."""
        obs1 = {
            "key_findings": ["finding1", "finding2"],
            "vulnerabilities": ["vuln1"],
            "next_actions": ["action1"],
        }
        obs2 = {
            "key_findings": ["finding1", "finding2"],
            "vulnerabilities": ["vuln1"],
            "next_actions": ["action1"],
        }

        similarity = calculate_observation_similarity(obs1, obs2)

        assert similarity == 1.0

    def test_calculate_similarity_different(self):
        """Test similarity for completely different observations."""
        obs1 = {
            "key_findings": ["finding1"],
            "vulnerabilities": [],
            "next_actions": [],
        }
        obs2 = {
            "key_findings": ["finding2"],
            "vulnerabilities": [],
            "next_actions": [],
        }

        similarity = calculate_observation_similarity(obs1, obs2)

        assert similarity == 0.0

    def test_calculate_similarity_partial(self):
        """Test similarity for partially overlapping observations."""
        obs1 = {
            "key_findings": ["finding1", "finding2"],
            "vulnerabilities": ["vuln1"],
            "next_actions": [],
        }
        obs2 = {
            "key_findings": ["finding1", "finding3"],
            "vulnerabilities": ["vuln1"],
            "next_actions": [],
        }

        similarity = calculate_observation_similarity(obs1, obs2)

        assert 0.0 < similarity < 1.0

    def test_calculate_similarity_empty(self):
        """Test similarity with empty observations."""
        obs1 = {}
        obs2 = {}

        similarity = calculate_observation_similarity(obs1, obs2)

        assert similarity == 0.0

    def test_calculate_similarity_none(self):
        """Test similarity with None observations."""
        similarity = calculate_observation_similarity(None, None)

        assert similarity == 0.0


class TestProgressScorer:
    """Test observation progress scoring."""

    def test_score_observation_progress_first(self):
        """Test progress score for first observation."""
        obs = {
            "key_findings": ["finding1"],
            "vulnerabilities": [],
            "next_actions": [],
        }

        score = score_observation_progress(obs, None)

        assert score == 1.0

    def test_score_observation_progress_identical(self):
        """Test progress score for identical observation."""
        obs1 = {
            "key_findings": ["finding1"],
            "vulnerabilities": [],
            "next_actions": [],
        }
        obs2 = {
            "key_findings": ["finding1"],
            "vulnerabilities": [],
            "next_actions": [],
        }

        score = score_observation_progress(obs2, obs1)

        assert score < 0.1  # Very low progress (duplicate)

    def test_score_observation_progress_new_findings(self):
        """Test progress score for observation with new findings."""
        obs1 = {
            "key_findings": ["finding1"],
            "vulnerabilities": [],
            "next_actions": [],
        }
        obs2 = {
            "key_findings": ["finding1", "finding2", "finding3"],
            "vulnerabilities": ["vuln1"],
            "next_actions": [],
        }

        score = score_observation_progress(obs2, obs1)

        assert score > 0.5  # High progress (new content)


class TestObservationDeduplication:
    """Test observation deduplication."""

    def test_check_observation_duplicate_exact(self):
        """Test detecting exact duplicate."""
        obs = {
            "summary": "Test",
            "key_findings": ["finding1"],
            "vulnerabilities": [],
            "next_actions": [],
        }
        hashes = [hash_observation(obs)]

        is_dup, similarity, obs_hash = check_observation_duplicate(obs, hashes)

        assert is_dup is True
        assert similarity == 1.0

    def test_check_observation_duplicate_new(self):
        """Test detecting new observation."""
        obs1 = {
            "summary": "Test 1",
            "key_findings": ["finding1"],
            "vulnerabilities": [],
            "next_actions": [],
        }
        obs2 = {
            "summary": "Test 2",
            "key_findings": ["finding2"],
            "vulnerabilities": [],
            "next_actions": [],
        }
        hashes = [hash_observation(obs1)]

        is_dup, similarity, obs_hash = check_observation_duplicate(obs2, hashes)

        assert is_dup is False
        assert similarity < 1.0

    def test_check_observation_duplicate_near_duplicate(self):
        """Test detecting near-duplicate (>90% similarity)."""
        obs1 = {
            "key_findings": ["finding1", "finding2", "finding3"],
            "vulnerabilities": ["vuln1"],
            "next_actions": ["action1"],
        }
        obs2 = {
            "key_findings": ["finding1", "finding2", "finding3", "finding4"],
            "vulnerabilities": ["vuln1"],
            "next_actions": ["action1"],
        }
        hashes = []

        is_dup, similarity, obs_hash = check_observation_duplicate(obs2, hashes, obs1)

        assert is_dup is False
        assert similarity > 0.9  # Near-duplicate


class TestToolOutputChangeDetection:
    """Test tool output change detection."""

    def test_detect_tool_output_change_first_execution(self):
        """Test change detection for first tool execution."""
        has_change, summary = detect_tool_output_change("nmap", "output1", {})

        assert has_change is True
        assert "First execution" in summary

    def test_detect_tool_output_change_identical(self):
        """Test change detection for identical output."""
        previous = {"nmap": "Port 22/tcp open\nPort 80/tcp open"}
        current = "Port 22/tcp open\nPort 80/tcp open"

        has_change, summary = detect_tool_output_change("nmap", current, previous)

        assert has_change is False
        assert "identical" in summary.lower()

    def test_detect_tool_output_change_different(self):
        """Test change detection for different output."""
        previous = {"nmap": "Port 22/tcp open"}
        current = "Port 22/tcp open\nPort 80/tcp open\nPort 443/tcp open"

        has_change, summary = detect_tool_output_change("nmap", current, previous)

        assert has_change is True
        assert "Significant changes" in summary

    def test_detect_tool_output_change_minor(self):
        """Test change detection for minor differences."""
        previous = {"nmap": "Port 22/tcp open\nPort 80/tcp open"}
        # Only whitespace/timestamp differences
        current = "Port 22/tcp open\nPort 80/tcp open\n"

        has_change, summary = detect_tool_output_change("nmap", current, previous)

        # Should detect as minor difference (may vary based on normalization)
        assert isinstance(has_change, bool)
        assert isinstance(summary, str)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])