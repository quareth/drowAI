"""Contract tests for the built-in Webweaver subagent and its skills."""

from agent.subagents.registry import get_subagent_registry
from agent.subagents.runtime.profile import resolve_subagent_tool_profile
from agent.subagents.skill_catalog import project_subagent_skill_catalogs
from agent.tools.universal_agent_tools import UNIVERSAL_AGENT_TOOL_IDS
from core.skills.registry import get_skill_registry
from core.skills.resolver import resolve_skills


WEBWEAVER_TOOL_IDS = (
    "information_gathering.web_enumeration.http_request",
    "web_applications.web_crawlers.ffuf",
)


def test_webweaver_uses_only_compatible_native_web_recon_tools() -> None:
    definition = get_subagent_registry().require("webweaver")
    profile = resolve_subagent_tool_profile(definition)

    assert definition.kind == "web_recon"
    assert definition.tool_ids == WEBWEAVER_TOOL_IDS
    assert profile.tool_ids == (*WEBWEAVER_TOOL_IDS, *UNIVERSAL_AGENT_TOOL_IDS)
    assert profile.capabilities_for_tool(WEBWEAVER_TOOL_IDS[0]) == (
        "web_reconnaissance",
    )
    assert profile.capabilities_for_tool(WEBWEAVER_TOOL_IDS[1]) == (
        "content_discovery",
    )


def test_webweaver_catalog_exposes_selectable_shell_skills() -> None:
    definitions = get_subagent_registry().definitions()
    skills = get_skill_registry()
    catalog = next(
        catalog
        for catalog in project_subagent_skill_catalogs(definitions, skills)
        if catalog.agent_id == "webweaver"
    )

    assert catalog.mandatory_skills == ()
    assert tuple(entry.skill_id for entry in catalog.selectable_skills) == (
        "katana",
        "whatweb",
    )

    resolution = resolve_skills(
        skills.skills(),
        "webweaver",
        ("whatweb", "katana"),
    )
    assert tuple(ref.skill_id for ref in resolution.selected) == (
        "whatweb",
        "katana",
    )
    assert all(ref.reasons == ("agent_selected",) for ref in resolution.selected)
    assert resolution.rejected_requests == ()
