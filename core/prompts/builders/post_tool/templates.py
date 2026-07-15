"""Template loading for the post-tool prompt builder family.

This module owns the versioned prompt text loaded for post-tool prompt
assembly, including extracted policy prose blocks.
"""

from __future__ import annotations

from pathlib import Path

from core.prompts.loader import TemplateLoader


_TEMPLATE_LOADER = TemplateLoader(Path(__file__).resolve().parents[2] / "versions")

SYSTEM_PROMPT = _TEMPLATE_LOADER.load_latest_version("post_tool", "system.txt")
ARTICULATION_SYSTEM_PROMPT = _TEMPLATE_LOADER.load_latest_version(
    "post_tool", "articulation_system.txt"
)
TASK_INSTRUCTION_PROMPT = _TEMPLATE_LOADER.load_latest_version(
    "post_tool", "task_instruction.txt"
)
DIRECT_EXECUTOR_POLICY_TEXT = _TEMPLATE_LOADER.load_latest_version(
    "post_tool", "direct_executor_policy.txt"
).rstrip("\n")
CVE_LOOKUP_GUIDANCE_TEXT = _TEMPLATE_LOADER.load_latest_version(
    "post_tool", "cve_lookup_guidance.txt"
).rstrip("\n")


__all__ = [
    "ARTICULATION_SYSTEM_PROMPT",
    "CVE_LOOKUP_GUIDANCE_TEXT",
    "DIRECT_EXECUTOR_POLICY_TEXT",
    "SYSTEM_PROMPT",
    "TASK_INSTRUCTION_PROMPT",
]
