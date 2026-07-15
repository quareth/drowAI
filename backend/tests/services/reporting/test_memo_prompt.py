"""Tests for task closure memo prompt rendering."""

from __future__ import annotations

import ast
import json
from pathlib import Path

from backend.services.reporting.evidence_packet_builder import (
    EvidencePacket,
    EvidencePacketItem,
)
from backend.services.reporting.knowledge_packet_builder import KnowledgePacket
from backend.services.reporting.memo_prompt import TaskClosureMemoPromptRenderer
from backend.services.reporting.runtime_readiness_service import RuntimeReadiness
from backend.services.reporting.task_memo_context_builder import (
    TaskMemoContext,
    TaskMemoTaskMetadata,
)
from backend.services.reporting.transcript_context_builder import (
    TranscriptContext,
    TranscriptContextItem,
)


def _context_with_oversized_source_text() -> TaskMemoContext:
    transcript_text = "transcript-start-" + ("A" * 5_000) + "-transcript-end"
    evidence_excerpt = "RAW_TOOL_OUTPUT:" + ("B" * 5_000) + "-evidence-end"
    return TaskMemoContext(
        task=TaskMemoTaskMetadata(
            task_id=42,
            tenant_id=7,
            user_id=8,
            engagement_id=9,
            name="Task 42",
            description="Task description",
            scope="Task scope",
            status="stopped",
            created_at="2026-06-09T09:00:00Z",
            stopped_at="2026-06-09T10:00:00Z",
        ),
        source_watermark={"schema_version": 1, "empty": False},
        transcript=TranscriptContext(
            task_id=42,
            conversation_id="conv-42",
            items=(
                TranscriptContextItem(
                    ref="message:1",
                    source="message",
                    role="assistant",
                    text=transcript_text,
                    created_at="2026-06-09T09:30:00Z",
                    turn_number=1,
                ),
            ),
            message_count=1,
            detail_event_count=0,
            total_characters=len(transcript_text),
            truncated=False,
            max_messages=80,
            max_characters=12_000,
        ),
        knowledge=KnowledgePacket(
            task_id=42,
            items=(),
            canonical_item_count=0,
            observation_item_count=0,
            candidate_item_count=0,
            truncated=False,
            max_items=120,
        ),
        evidence=EvidencePacket(
            task_id=42,
            items=(
                EvidencePacketItem(
                    ref="evidence_archive:abc",
                    evidence_id="abc",
                    tenant_id=7,
                    user_id=8,
                    engagement_id=9,
                    task_id=42,
                    source_execution_id="exec-1",
                    source_artifact_id="artifact-1",
                    observed_at="2026-06-09T09:40:00Z",
                    created_at="2026-06-09T09:41:00Z",
                    source_tool="nmap",
                    evidence_type="service",
                    target="10.0.0.5:443",
                    summary="TLS service observed.",
                    excerpt=evidence_excerpt,
                    excerpt_source="inline_excerpt",
                    excerpt_truncated=False,
                    linked_asset_refs=(),
                    linked_service_refs=(),
                    linked_finding_refs=(),
                    byte_size=None,
                    mime_type="text/plain",
                ),
            ),
            item_count=1,
            artifact_fallback_count=0,
            total_excerpt_characters=len(evidence_excerpt),
            truncated=False,
            max_items=80,
            max_excerpt_characters=1_500,
            max_total_characters=12_000,
        ),
        previous_memo=None,
        runtime_readiness=RuntimeReadiness(
            runtime_retired=True,
            useful_runtime_execution=True,
            not_preparable_reason=None,
        ),
        memo_mode="supported",
        not_preparable_reason=None,
        allowed_evidence_refs=frozenset({"evidence_archive:abc"}),
        allowed_knowledge_refs=frozenset(),
    )


def test_renderer_resolves_registry_templates_and_metadata() -> None:
    rendered = TaskClosureMemoPromptRenderer().render(
        _context_with_oversized_source_text()
    )

    assert "task closure memo generator" in rendered.system_prompt
    assert "<TASK_CLOSURE_MEMO_CONTEXT_JSON>" in rendered.user_prompt
    assert rendered.metadata == {
        "prompt_family": "task_closure_memo",
        "prompt_version": "v1",
        "prompt_template_ids": [
            "task_closure_memo_system",
            "task_closure_memo_user",
        ],
    }


def test_renderer_embeds_only_bounded_packet_json_in_user_context_block() -> None:
    rendered = TaskClosureMemoPromptRenderer().render(
        _context_with_oversized_source_text()
    )

    assert "A" * 5_000 not in rendered.user_prompt
    assert "B" * 5_000 not in rendered.user_prompt
    assert "RAW_TOOL_OUTPUT" not in rendered.user_prompt
    assert "-transcript-end" not in rendered.user_prompt
    assert "-evidence-end" not in rendered.user_prompt
    assert "...[truncated]" in rendered.user_prompt

    payload = json.loads(rendered.memo_context_json)
    assert payload["task"]["task_id"] == 42
    assert payload["memo_mode"] == "supported"
    assert payload["transcript_context"]["items"][0]["text"].endswith(
        "...[truncated]"
    )
    evidence_item = payload["evidence_packet"]["items"][0]
    assert "excerpt" not in evidence_item
    assert "summary" not in evidence_item
    assert evidence_item["tool_display_name"] == "Nmap"
    assert evidence_item["ref"] == "evidence_archive:abc"
    assert payload["allowed_evidence_refs"] == ["evidence_archive:abc"]


def test_memo_prompt_module_imports_prompt_registry_without_llm_clients() -> None:
    path = Path("backend/services/reporting/memo_prompt.py")
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imported_modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_modules.add(node.module)

    assert "core.prompts.registry" in imported_modules
    assert not {
        module
        for module in imported_modules
        if module.startswith(
            (
                "agent.providers.llm",
                "backend.services.llm_provider",
                "core.llm",
                "openai",
                "anthropic",
            )
        )
    }
