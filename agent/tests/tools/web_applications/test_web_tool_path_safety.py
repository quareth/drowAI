"""Path safety regression tests for web application tool CLI helpers."""

from pathlib import Path

import pytest

from agent.tools.web_applications._path_safety import (
    is_allowed_system_wordlist,
    resolve_wordlist_path_for_execution,
)


def test_system_wordlist_roots_are_allowed() -> None:
    """Known Kali wordlist roots may be referenced as absolute paths."""

    for wordlist in (
        "/usr/share/wordlists/dirb/common.txt",
        "/usr/share/seclists/Discovery/Web-Content/common.txt",
        "/usr/share/skipfish/dictionaries/minimal.wl",
    ):
        assert is_allowed_system_wordlist(wordlist)
        assert resolve_wordlist_path_for_execution(wordlist) == str(
            Path(wordlist).resolve(strict=False)
        )


def test_system_wordlist_allowlist_rejects_path_traversal() -> None:
    """Absolute paths must remain inside the approved roots after resolution."""

    for wordlist in (
        "/usr/share/wordlists/../../etc/passwd",
        "/usr/share/seclists/../../etc/shadow",
        "/usr/share/wordlists_evil/common.txt",
        "/tmp/list.txt",
    ):
        assert not is_allowed_system_wordlist(wordlist)
        with pytest.raises(ValueError):
            resolve_wordlist_path_for_execution(wordlist)
