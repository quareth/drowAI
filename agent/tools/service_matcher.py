from __future__ import annotations

"""Service-aware tool selection utilities."""

from typing import Dict, List  # noqa: E402
from pydantic import BaseModel, Field  # noqa: E402


class ServiceInfo(BaseModel):
    """Information about a discovered service."""

    name: str
    port: int
    protocol: str = "tcp"
    version: str = ""
    technology: str = ""
    banner: str = ""


class ServiceInventory(BaseModel):
    """Collection of discovered services."""

    services: Dict[str, ServiceInfo] = Field(default_factory=dict)

    def add_service(self, service: ServiceInfo) -> None:
        """Add a service to the inventory."""

        key = f"{service.name}_{service.port}_{service.protocol}"
        self.services[key] = service

    def get_services_by_name(self, name: str) -> List[ServiceInfo]:
        """Get all services with the given name."""

        return [svc for svc in self.services.values() if svc.name == name]


class ServiceAwareSelector:
    """Selects tools based on discovered service information."""

    def __init__(self) -> None:
        self.service_specific_tools: Dict[str, Dict[str, List[str]]] = {
            "http": {
                "enumeration": [
                    "web_applications.web_crawlers.gobuster",
                    "web_applications.web_crawlers.dirb",
                ],
                "vulnerability_analysis": [
                    "web_applications.web_application_proxies.zaproxy"
                ],
            },
            "https": {
                "enumeration": [
                    "web_applications.web_crawlers.gobuster",
                    "web_applications.web_crawlers.dirb",
                ],
                "vulnerability_analysis": [
                    "web_applications.web_application_proxies.zaproxy"
                ],
            },
            "dns": {
                "enumeration": [
                    "information_gathering.dns.dnsrecon",
                    "information_gathering.dns.dnsenum",
                    "information_gathering.dns.fierce",
                ]
            },
            "smtp": {
                "enumeration": [
                    "information_gathering.smtp_analysis.smtp_user_enum",
                    "information_gathering.smtp_analysis.swaks",
                ]
            },
            "tcp": {
                "enumeration": [
                    "information_gathering.network_discovery.nmap",
                    "information_gathering.network_discovery.masscan",
                ]
            },
        }

    def get_tools_for_service(
        self, service: str, action_type: str, service_details: ServiceInfo | None = None
    ) -> List[str]:
        """Get tools appropriate for specific service and action."""

        service_tools = self.service_specific_tools.get(service, {})
        base_tools = service_tools.get(action_type)

        # Default to generic TCP tools when service unknown
        if base_tools is None:
            base_tools = self.service_specific_tools.get("tcp", {}).get(action_type, [])

        if not service_details:
            return list(base_tools)

        # Determine technology hints from provided details
        tech_source = (
            service_details.technology
            or service_details.version
            or service_details.banner
        )
        extra_tools: List[str] = []
        if service in {"http", "https"} and tech_source:
            tech_lower = tech_source.lower()
            if "wordpress" in tech_lower:
                extra_tools.append("web_applications.cms_identification.wpscan")
            elif "apache" in tech_lower:
                # Placeholder for potential Apache-specific tooling
                pass
            elif "nginx" in tech_lower:
                # Placeholder for potential Nginx-specific tooling
                pass

        # Preserve order and uniqueness
        combined = list(dict.fromkeys([*base_tools, *extra_tools]))
        return combined


__all__ = ["ServiceInfo", "ServiceInventory", "ServiceAwareSelector"]
