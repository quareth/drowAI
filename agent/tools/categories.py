from enum import Enum


class ToolCategory(str, Enum):
    """Broad categories describing tool functionality."""

    # Information Gathering
    NETWORK_DISCOVERY = "network_discovery"
    DNS_ENUMERATION = "dns_enumeration"
    WEB_ENUMERATION = "web_enumeration"

    # Web Applications
    WEB_CRAWLING = "web_crawling"
    WEB_VULNERABILITY_SCANNING = "web_vulnerability_scanning"
    WEB_FUZZING = "web_fuzzing"
    APPLICATION_PROXY = "application_proxy"
    CMS_IDENTIFICATION = "cms_identification"

    # Vulnerability Analysis
    DATABASE_ASSESSMENT = "database_assessment"
    OPENVAS_SCANNING = "openvas_scanning"
    FUZZING = "fuzzing"
    CISCO_TOOLS = "cisco_tools"
    VOIP_ANALYSIS = "voip_analysis"

    # Exploitation
    EXPLOITATION_TOOLS = "exploitation_tools"
    PASSWORD_ATTACKS = "password_attacks"

    # Network Operations
    SNIFFING_SPOOFING = "sniffing_spoofing"

    # Post-Exploitation
    REVERSE_ENGINEERING = "reverse_engineering"
    STRESS_TESTING = "stress_testing"
    FORENSICS = "forensics"
    MAINTAINING_ACCESS = "maintaining_access"

    # Utilities
    SYSTEM_SERVICES = "system_services"
    NETWORKING_UTILITIES = "networking_utilities"
    SERVICE_ACCESS = "service_access"
    REPORTING_TOOLS = "reporting_tools"
    KNOWLEDGE = "knowledge"
    WORKSPACE_FILESYSTEM = "workspace_filesystem"
    SHELL = "shell"


class PentestPhase(str, Enum):
    """Phases of a typical penetration test."""

    RECONNAISSANCE = "reconnaissance"
    ENUMERATION = "enumeration"
    VULNERABILITY_ASSESSMENT = "vulnerability_assessment"
    EXPLOITATION = "exploitation"
    PRIVILEGE_ESCALATION = "privilege_escalation"
    POST_EXPLOITATION = "post_exploitation"
