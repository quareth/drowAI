"""Regression guard: the UI transcript read model stays out of the hot path.

``backend.services.chat.transcript_query_service.ChatTranscriptQueryService``
is a UI/pagination read model and must never become the
prompt-authoritative recent-transcript source. Prompt-facing recent
transcript text is produced exclusively by the shared serializer in
``agent.graph.context.serialization`` (fed by
``ConversationHistoryReader.build_openai_conversation_history`` ->
``ConversationContextBundle``).

This test fails if any wired hot-path prompt-assembly module starts
importing the UI read model, which would introduce a second
conversation-history authority, couple prompt assembly to UI
concerns, and break cache-prefix stability.
"""

from __future__ import annotations

from pathlib import Path
import re


_REPO_ROOT = Path(__file__).resolve().parents[3]

# Hot-path directories: prompt-authoritative assembly surfaces that the
# LangGraph runtime routes through on every turn. New hot-path modules
# added later should be appended here so the guard keeps tracking them.
_HOT_PATH_DIRS: tuple[Path, ...] = (
    _REPO_ROOT / "agent" / "graph",
    _REPO_ROOT / "backend" / "services" / "langgraph_chat",
    _REPO_ROOT / "core" / "prompts" / "builders",
)

_FORBIDDEN_PATTERN = re.compile(
    r"\b(ChatTranscriptQueryService|chat_transcript_query_service)\b"
)


def _python_files(directory: Path) -> list[Path]:
    if not directory.exists():
        return []
    return [
        path
        for path in directory.rglob("*.py")
        # Exclude ``tests/`` subtrees so assertions/regressions of the
        # rule itself (like this test) do not count as a hot-path use.
        if "tests" not in path.parts
    ]


def test_ui_transcript_read_model_is_not_imported_from_hot_path_modules() -> None:
    offenders: list[str] = []
    for hot_dir in _HOT_PATH_DIRS:
        for python_file in _python_files(hot_dir):
            contents = python_file.read_text(encoding="utf-8")
            if _FORBIDDEN_PATTERN.search(contents):
                offenders.append(str(python_file.relative_to(_REPO_ROOT)))

    assert not offenders, (
        "Hot-path prompt-assembly modules must not reference the UI "
        "transcript read model (ChatTranscriptQueryService / "
        "chat_transcript_query_service). Offenders: "
        f"{sorted(offenders)}. Use the shared serializer in "
        "agent.graph.context.serialization instead."
    )
