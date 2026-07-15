from __future__ import annotations

"""Context-aware mapping from actions to concrete tools."""

from typing import Any, Dict, List  # noqa: E402

from agent.models import ActionType  # noqa: E402
from .enhanced_metadata_registry import (  # noqa: E402
    get_enhanced_tool_metadata,
    get_all_enhanced_metadata,
)
from .tool_registry import available_tools  # noqa: E402
from .service_matcher import ServiceAwareSelector  # noqa: E402
from .categories import ToolCategory, PentestPhase  # noqa: E402


class ContextualToolSelector:
    """Selects appropriate tools for actions based on context."""

    def __init__(self) -> None:
        self.action_tool_map = self._build_action_tool_map()
        self.service_tool_map = self._build_service_tool_map()
        self.phase_tool_map = self._build_phase_tool_map()

    def select_tools_for_action(
        self, action_type: ActionType, context: Dict[str, Any]
    ) -> List[str]:
        """Select optimal tools for an action given current context."""

        base_tools = self._get_base_tools_for_action(action_type)
        # Debug context: action and base tool candidates
        try:
            import logging
            logging.getLogger(__name__).debug(
                f"ToolSelector: action={action_type.value} base_candidates={base_tools} context_keys={list(context.keys())}"
            )
        except Exception:
            pass
        service_filtered = self._filter_by_services(base_tools, context)
        phase_filtered = self._filter_by_phase(service_filtered, context)
        prioritized = self._prioritize_tools(phase_filtered)
        try:
            import logging
            logging.getLogger(__name__).debug(
                f"ToolSelector: prioritized={prioritized} (max={context.get('max_tools_per_action', 3)})"
            )
        except Exception:
            pass
        # Respect configuration knob if available in context
        max_tools = int(context.get("max_tools_per_action", 3))
        return prioritized[:max_tools]

    # ------------------------------------------------------------------
    # Mapping builders
    # ------------------------------------------------------------------
    def _build_action_tool_map(self) -> Dict[ActionType, List[str]]:
        """Build mapping from :class:`ActionType` to available tools using enhanced metadata."""

        # Preferred: derive from metadata by category
        metadata = get_all_enhanced_metadata()
        existing = set(available_tools())

        def tools_in_categories(categories: List[ToolCategory]) -> List[str]:
            cats = set(categories)
            return [
                tool_id
                for tool_id, meta in metadata.items()
                if meta.category in cats and tool_id in existing
            ]

        mapping: Dict[ActionType, List[str]] = {
            ActionType.SCAN_PORTS: tools_in_categories(
                [ToolCategory.NETWORK_DISCOVERY]
            ),
            ActionType.SCAN_WEB: tools_in_categories(
                [
                    ToolCategory.WEB_CRAWLING,
                    ToolCategory.WEB_VULNERABILITY_SCANNING,
                    ToolCategory.APPLICATION_PROXY,
                ]
            ),
            ActionType.ENUMERATE_SERVICES: tools_in_categories(
                [
                    ToolCategory.NETWORK_DISCOVERY,
                    ToolCategory.DNS_ENUMERATION,
                    ToolCategory.SYSTEM_SERVICES,
                ]
            ),
            ActionType.GATHER_INFO: tools_in_categories(
                [
                    ToolCategory.NETWORK_DISCOVERY,
                    ToolCategory.DNS_ENUMERATION,
                    ToolCategory.WEB_ENUMERATION,
                ]
            ),
            ActionType.TEST_EXPLOIT: tools_in_categories(
                [
                    ToolCategory.EXPLOITATION_TOOLS,
                    ToolCategory.WEB_VULNERABILITY_SCANNING,
                    ToolCategory.PASSWORD_ATTACKS,
                ]
            ),
            ActionType.GENERATE_REPORT: tools_in_categories(
                [ToolCategory.REPORTING_TOOLS]
            ),
        }

        # Fallback: if any action maps empty (due to missing metadata), keep existing validated hardcoded defaults
        # Build the previous validated mapping and merge for empties
        hardcoded: Dict[ActionType, List[str]] = {
            ActionType.SCAN_PORTS: [
                "information_gathering.network_discovery.nmap",
                "information_gathering.network_discovery.masscan",
                "information_gathering.network_discovery.unicornscan",
            ],
            ActionType.SCAN_WEB: [
                "web_applications.web_crawlers.gobuster",
                "web_applications.web_crawlers.dirb",
                "web_applications.web_vulnerability_scanners.nikto",
                "web_applications.web_vulnerability_scanners.wapiti",
                "web_applications.web_application_proxies.zaproxy",
            ],
            ActionType.ENUMERATE_SERVICES: [
                "information_gathering.network_discovery.nmap",
                "information_gathering.dns.dnsrecon",
                "information_gathering.dns.dnsenum",
                "information_gathering.smtp_analysis.smtp_user_enum",
            ],
            ActionType.GATHER_INFO: [
                "information_gathering.network_discovery.fping",
                "information_gathering.dns.fierce",
                "information_gathering.dns.amass",
                "information_gathering.osint.theharvester",
                "information_gathering.osint.shodan",
            ],
            ActionType.TEST_EXPLOIT: [
                "exploitation_tools.metasploit.run_exploit",
                "web_applications.web_vulnerability_scanners.sqlmap",
                "password_attacks.online_attacks.hydra",
            ],
            ActionType.GENERATE_REPORT: [
                "reporting_tools.report_generation.serpico",
            ],
        }
        validated_hardcoded: Dict[ActionType, List[str]] = {}
        for action, tools in hardcoded.items():
            validated_hardcoded[action] = [tool for tool in tools if tool in existing]

        for action, tools in list(mapping.items()):
            if not tools:
                mapping[action] = validated_hardcoded.get(action, [])
        return mapping

    def _build_service_tool_map(self) -> Dict[str, List[str]]:
        """Build mapping from service names to appropriate tools, sourced from ServiceAwareSelector."""

        selector = ServiceAwareSelector()
        # Flatten the selector's internal map into a simple service->tools list for the common 'enumeration' action
        service_map: Dict[str, List[str]] = {}
        for service, actions in selector.service_specific_tools.items():
            tools = list(dict.fromkeys(actions.get("enumeration", [])))
            if tools:
                service_map[service] = tools
        # Ensure a generic TCP fallback exists
        service_map.setdefault(
            "tcp",
            [
                "information_gathering.network_discovery.nmap",
                "information_gathering.network_discovery.masscan",
                "information_gathering.network_discovery.unicornscan",
            ],
        )
        return service_map

    def _build_phase_tool_map(self) -> Dict[str, List[str]]:
        """Build mapping from pentest phases to appropriate tools using enhanced metadata."""

        metadata = get_all_enhanced_metadata()
        existing = set(available_tools())
        phase_map: Dict[str, List[str]] = {}
        for tool_id, meta in metadata.items():
            if tool_id not in existing:
                continue
            for phase in meta.applicable_phases or []:
                key = phase.value if isinstance(phase, PentestPhase) else str(phase)
                phase_map.setdefault(key, []).append(tool_id)

        # Preserve deterministic ordering by priority desc where possible
        for key, tools in list(phase_map.items()):
            def prio(tid: str) -> int:
                m = metadata.get(tid)
                return m.execution_priority if m else 0
            phase_map[key] = sorted(tools, key=prio, reverse=True)
        return phase_map

    # ------------------------------------------------------------------
    # Filtering helpers
    # ------------------------------------------------------------------
    def _get_base_tools_for_action(self, action_type: ActionType) -> List[str]:
        return self.action_tool_map.get(action_type, [])

    def _filter_by_services(
        self, tools: List[str], context: Dict[str, Any]
    ) -> List[str]:
        services = context.get("discovered_services", {})
        if not services:
            return tools

        available = set(services.keys())
        filtered: List[str] = []
        for tool_id in tools:
            metadata = get_enhanced_tool_metadata(tool_id)
            if not metadata or not metadata.required_services:
                filtered.append(tool_id)
            elif any(req in available for req in metadata.required_services):
                filtered.append(tool_id)
        return filtered

    def _filter_by_phase(self, tools: List[str], context: Dict[str, Any]) -> List[str]:
        phase = context.get("current_phase")
        if not phase:
            return tools

        filtered: List[str] = []
        for tool_id in tools:
            metadata = get_enhanced_tool_metadata(tool_id)
            if not metadata or not metadata.applicable_phases:
                filtered.append(tool_id)
            else:
                phases = {p.value for p in metadata.applicable_phases}
                if phase in phases:
                    filtered.append(tool_id)
        return filtered

    def _prioritize_tools(self, tools: List[str]) -> List[str]:
        def priority(tool_id: str) -> int:
            metadata = get_enhanced_tool_metadata(tool_id)
            return metadata.execution_priority if metadata else 0

        return sorted(tools, key=priority, reverse=True)


__all__ = ["ContextualToolSelector"]
