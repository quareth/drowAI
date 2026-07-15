"""Integration-style checks for password attack tools registration and loading."""

from agent.tools.enhanced_metadata_registry import get_enhanced_tool_metadata
from agent.tools.tool_registry import get_tool


PASSWORD_ATTACK_TOOL_IDS = [
    "password_attacks.online_attacks.ncrack",
    "password_attacks.online_attacks.crowbar",
    "password_attacks.online_attacks.patator",
    "password_attacks.offline_attacks.rainbowcrack",
    "password_attacks.offline_attacks.samdump2",
    "password_attacks.offline_attacks.crunch",
    "password_attacks.passing_the_hash.ntlmrelayx",
    "password_attacks.passing_the_hash.passing_the_hash_toolkit",
]


def test_tools_load_from_registry() -> None:
    """Ensure each password attack tool can be resolved via the registry."""
    for tool_id in PASSWORD_ATTACK_TOOL_IDS:
        tool_cls = get_tool(tool_id)
        assert tool_cls is not None, f"{tool_id} not loadable from registry"


def test_enhanced_metadata_registered() -> None:
    """Ensure enhanced metadata is available for all password attack tools."""
    for tool_id in PASSWORD_ATTACK_TOOL_IDS:
        metadata = get_enhanced_tool_metadata(tool_id)
        assert metadata is not None, f"{tool_id} missing enhanced metadata"


