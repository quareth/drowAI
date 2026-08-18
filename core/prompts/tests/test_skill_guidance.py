"""Tests for subordinate built-in skill system-prompt rendering."""

from core.prompts.builders.skill_guidance import PromptSkill, render_skill_guidance


def test_empty_skill_guidance_has_no_header() -> None:
    assert render_skill_guidance(()) == ""


def test_skill_guidance_renders_one_authority_section() -> None:
    rendered = render_skill_guidance(
        (
            PromptSkill(
                skill_id="network-reconnaissance",
                description="Bounded discovery.",
                body="# Method\nPreserve scope.",
            ),
        )
    )

    assert rendered.count("Specialized Capability Guidance:") == 1
    assert "does not add tools, permissions, targets, or authority" in rendered
    assert '<skill id="network-reconnaissance">' in rendered
    assert "# Method\nPreserve scope." in rendered
