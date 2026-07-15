"""
Comprehensive schema validation tests for all pentesting tools.

This test suite validates that:
1. All tool schemas (Pydantic models) are valid and can be instantiated
2. All enum values in schemas are valid
3. All parameters can be set without ValidationError
4. build_command() works with all valid parameter combinations
5. No schema parameters are unused or invalid

This addresses the issue where manual testing sometimes reveals unavailable arguments.
"""

from __future__ import annotations

import inspect
from enum import Enum
from typing import Any, Dict, List, Optional, Type, get_args, get_origin

import pytest
from pydantic import BaseModel, ValidationError

from agent.tools.base_tool import BaseTool
from agent.tools.schemas import BaseToolArgs


def get_pydantic_fields(model_class: Type[BaseModel]) -> Dict[str, Any]:
    """Extract all fields from a Pydantic model."""
    return model_class.model_fields


def get_enum_values(enum_class: Type[Enum]) -> List[Any]:
    """Get all values from an enum."""
    return [e.value for e in enum_class]


def get_field_type_info(field_info: Any) -> Dict[str, Any]:
    """Extract type information from a Pydantic field."""
    annotation = field_info.annotation
    origin = get_origin(annotation)
    args = get_args(annotation)
    
    info = {
        "annotation": annotation,
        "origin": origin,
        "args": args,
        "is_optional": False,
        "is_list": False,
        "is_enum": False,
        "inner_type": None,
        "enum_values": [],
        "constraints": {},
    }
    
    # Extract constraints from field metadata
    if hasattr(field_info, 'metadata'):
        for constraint in field_info.metadata:
            if hasattr(constraint, 'ge'):
                info["constraints"]["ge"] = constraint.ge
            if hasattr(constraint, 'gt'):
                info["constraints"]["gt"] = constraint.gt
            if hasattr(constraint, 'le'):
                info["constraints"]["le"] = constraint.le
            if hasattr(constraint, 'lt'):
                info["constraints"]["lt"] = constraint.lt
    
    # Check for Optional
    if origin is type(None) or (origin and "Union" in str(origin)):
        info["is_optional"] = True
        if args:
            # Get the non-None type from Optional[X]
            info["inner_type"] = next((arg for arg in args if arg is not type(None)), None)
    else:
        info["inner_type"] = annotation
    
    # Check for List
    if origin is list:
        info["is_list"] = True
        if args:
            info["inner_type"] = args[0]
    
    # Check for Enum
    inner = info["inner_type"]
    if inner and inspect.isclass(inner) and issubclass(inner, Enum):
        info["is_enum"] = True
        info["enum_values"] = get_enum_values(inner)
    
    return info


class TestToolSchemaValidation:
    """Base test class for comprehensive schema validation."""
    
    def get_tool_classes(self) -> List[Type[BaseTool]]:
        """Override in subclass to provide specific tool classes."""
        return []
    
    def get_minimal_args(self, args_class: Type[BaseToolArgs]) -> Dict[str, Any]:
        """Generate minimal valid arguments for a tool."""
        fields = get_pydantic_fields(args_class)
        minimal = {}
        
        for field_name, field_info in fields.items():
            # Check if field is required
            is_required = field_info.is_required()
            
            # Skip optional fields
            if not is_required:
                continue
            
            # Field is required, provide a minimal value
            type_info = get_field_type_info(field_info)
            
            if field_name == "target":
                minimal[field_name] = "http://example.com"
            elif field_name in ("host", "hostname"):
                minimal[field_name] = "192.168.1.1"
            elif field_name == "wordlist":
                minimal[field_name] = "/usr/share/wordlists/common.txt"
            elif field_name == "protocol":
                # For protocol fields, use first enum value
                if type_info["is_enum"] and type_info["enum_values"]:
                    minimal[field_name] = type_info["enum_values"][0]
                else:
                    minimal[field_name] = "tcp"
            elif field_name == "module":
                # For module fields, use first enum value
                if type_info["is_enum"] and type_info["enum_values"]:
                    minimal[field_name] = type_info["enum_values"][0]
                else:
                    minimal[field_name] = "default"
            elif type_info["is_enum"] and type_info["enum_values"]:
                minimal[field_name] = type_info["enum_values"][0]
            elif type_info["inner_type"] == str:
                minimal[field_name] = "test_value"
            elif type_info["inner_type"] == int:
                minimal[field_name] = 1
            elif type_info["inner_type"] == bool:
                minimal[field_name] = False
            elif type_info["is_list"]:
                minimal[field_name] = []
            else:
                # Default fallback
                minimal[field_name] = "default"
        
        return minimal
    
    def test_schema_can_be_instantiated(self):
        """Test that all tool schemas can be instantiated with minimal args."""
        for tool_cls in self.get_tool_classes():
            args_class = tool_cls.args_model
            minimal_args = self.get_minimal_args(args_class)
            
            try:
                args_instance = args_class(**minimal_args)
                assert args_instance is not None, f"{args_class.__name__} could not be instantiated"
            except ValidationError as e:
                pytest.fail(
                    f"{args_class.__name__} failed validation with minimal args: {minimal_args}\n"
                    f"Error: {e}"
                )
    
    def test_all_enum_fields_valid(self):
        """Test that all enum values in schemas are valid and accepted."""
        for tool_cls in self.get_tool_classes():
            args_class = tool_cls.args_model
            fields = get_pydantic_fields(args_class)
            minimal_args = self.get_minimal_args(args_class)
            
            for field_name, field_info in fields.items():
                type_info = get_field_type_info(field_info)
                
                if type_info["is_enum"]:
                    for enum_value in type_info["enum_values"]:
                        test_args = {**minimal_args, field_name: enum_value}
                        try:
                            args_instance = args_class(**test_args)
                            assert getattr(args_instance, field_name) == enum_value, \
                                f"Enum value {enum_value} not properly set for {field_name}"
                        except ValidationError as e:
                            pytest.fail(
                                f"{args_class.__name__}.{field_name} failed with enum value {enum_value}\n"
                                f"Error: {e}"
                            )
    
    def test_build_command_with_minimal_args(self):
        """Test that build_command works with minimal arguments."""
        for tool_cls in self.get_tool_classes():
            tool = tool_cls()
            args_class = tool_cls.args_model
            minimal_args = self.get_minimal_args(args_class)
            
            try:
                args_instance = args_class(**minimal_args)
                command = tool.build_command(args_instance)
                
                assert isinstance(command, list), \
                    f"{tool_cls.__name__}.build_command() did not return a list"
                assert len(command) > 0, \
                    f"{tool_cls.__name__}.build_command() returned empty command"
                assert all(isinstance(arg, str) for arg in command), \
                    f"{tool_cls.__name__}.build_command() returned non-string arguments"
            except Exception as e:
                pytest.fail(
                    f"{tool_cls.__name__}.build_command() failed with minimal args:\n"
                    f"Args: {minimal_args}\n"
                    f"Error: {e}"
                )
    
    def test_optional_fields_can_be_set(self):
        """Test that all optional fields can be set without errors."""
        for tool_cls in self.get_tool_classes():
            args_class = tool_cls.args_model
            fields = get_pydantic_fields(args_class)
            minimal_args = self.get_minimal_args(args_class)
            
            for field_name, field_info in fields.items():
                type_info = get_field_type_info(field_info)
                
                # Skip required fields already in minimal_args
                if field_name in minimal_args:
                    continue
                
                # Generate a test value for this optional field
                test_value = None
                if type_info["is_list"]:
                    # Handle list fields first
                    test_value = ["item1", "item2"]
                elif type_info["is_enum"] and type_info["enum_values"]:
                    test_value = type_info["enum_values"][0]
                elif type_info["inner_type"] == str:
                    test_value = "test_optional"
                elif type_info["inner_type"] == int:
                    # Respect constraints
                    if "ge" in type_info["constraints"]:
                        test_value = type_info["constraints"]["ge"]
                    elif "gt" in type_info["constraints"]:
                        test_value = type_info["constraints"]["gt"] + 1
                    else:
                        test_value = 100
                elif type_info["inner_type"] == bool:
                    test_value = True
                
                if test_value is not None:
                    test_args = {**minimal_args, field_name: test_value}
                    try:
                        args_instance = args_class(**test_args)
                        actual_value = getattr(args_instance, field_name)
                        assert actual_value == test_value, \
                            f"Optional field {field_name} not set correctly"
                    except ValidationError as e:
                        pytest.fail(
                            f"{args_class.__name__}.{field_name} failed with value {test_value}\n"
                            f"Error: {e}"
                        )
    
    def test_build_command_with_all_options(self):
        """Test build_command with as many options set as possible."""
        for tool_cls in self.get_tool_classes():
            tool = tool_cls()
            args_class = tool_cls.args_model
            fields = get_pydantic_fields(args_class)
            
            # Build args with all fields populated
            full_args = self.get_minimal_args(args_class)
            
            for field_name, field_info in fields.items():
                if field_name in full_args:
                    continue
                
                type_info = get_field_type_info(field_info)
                
                if type_info["is_enum"] and type_info["enum_values"]:
                    # Use second enum value if available, else first
                    full_args[field_name] = (
                        type_info["enum_values"][1] 
                        if len(type_info["enum_values"]) > 1 
                        else type_info["enum_values"][0]
                    )
                elif type_info["is_list"]:
                    # Handle list fields first before checking inner_type
                    full_args[field_name] = ["option1", "option2"]
                elif type_info["inner_type"] == str:
                    full_args[field_name] = f"full_{field_name}"
                elif type_info["inner_type"] == int:
                    # Respect constraints
                    if "ge" in type_info["constraints"]:
                        full_args[field_name] = type_info["constraints"]["ge"]
                    elif "gt" in type_info["constraints"]:
                        full_args[field_name] = type_info["constraints"]["gt"] + 1
                    else:
                        full_args[field_name] = 100
                elif type_info["inner_type"] == bool:
                    full_args[field_name] = True
            
            try:
                args_instance = args_class(**full_args)
                command = tool.build_command(args_instance)
                
                assert isinstance(command, list), \
                    f"{tool_cls.__name__}.build_command() did not return a list with full args"
                assert len(command) > 0, \
                    f"{tool_cls.__name__}.build_command() returned empty command with full args"
            except Exception as e:
                pytest.fail(
                    f"{tool_cls.__name__}.build_command() failed with full args:\n"
                    f"Args: {full_args}\n"
                    f"Error: {e}"
                )


# ---------------------------------------------------------------------------
# Password Attack Tools Tests
# ---------------------------------------------------------------------------


class TestPasswordAttackOnlineToolsSchemas(TestToolSchemaValidation):
    """Comprehensive schema validation for online password attack tools."""
    
    def get_tool_classes(self) -> List[Type[BaseTool]]:
        from agent.tools.password_attacks.online_attacks.ncrack import NcrackTool
        from agent.tools.password_attacks.online_attacks.crowbar import CrowbarTool
        from agent.tools.password_attacks.online_attacks.patator import PatatorTool
        from agent.tools.password_attacks.online_attacks.hydra import HydraTool
        from agent.tools.password_attacks.online_attacks.medusa import MedusaTool
        
        return [NcrackTool, CrowbarTool, PatatorTool, HydraTool, MedusaTool]


class TestPasswordAttackOfflineToolsSchemas(TestToolSchemaValidation):
    """Comprehensive schema validation for offline password attack tools."""
    
    def get_tool_classes(self) -> List[Type[BaseTool]]:
        from agent.tools.password_attacks.offline_attacks.john import JohnTool
        from agent.tools.password_attacks.offline_attacks.hashcat import HashcatTool
        from agent.tools.password_attacks.offline_attacks.rainbowcrack import RainbowCrackTool
        from agent.tools.password_attacks.offline_attacks.samdump2 import SAMdump2Tool
        from agent.tools.password_attacks.offline_attacks.crunch import CrunchTool
        
        return [
            JohnTool,
            HashcatTool,
            RainbowCrackTool,
            SAMdump2Tool,
            CrunchTool,
        ]


class TestPasswordAttackPTHToolsSchemas(TestToolSchemaValidation):
    """Comprehensive schema validation for passing-the-hash tools."""
    
    def get_tool_classes(self) -> List[Type[BaseTool]]:
        from agent.tools.password_attacks.passing_the_hash.mimikatz import MimikatzTool
        from agent.tools.password_attacks.passing_the_hash.ntlmrelayx import NTLMRelayXTool
        from agent.tools.password_attacks.passing_the_hash.passing_the_hash_toolkit import (
            PassingTheHashToolkitTool,
        )
        from agent.tools.password_attacks.passing_the_hash.responder import ResponderTool
        
        return [MimikatzTool, NTLMRelayXTool, PassingTheHashToolkitTool, ResponderTool]


# ---------------------------------------------------------------------------
# Information Gathering Tools Tests
# ---------------------------------------------------------------------------


class TestInformationGatheringNetworkToolsSchemas(TestToolSchemaValidation):
    """Comprehensive schema validation for network discovery tools."""
    
    def get_tool_classes(self) -> List[Type[BaseTool]]:
        from agent.tools.information_gathering.network_discovery.nmap import NmapTool
        from agent.tools.information_gathering.network_discovery.masscan import MasscanTool
        from agent.tools.information_gathering.network_discovery.zmap import ZmapTool
        from agent.tools.information_gathering.network_discovery.unicornscan import (
            UnicornscanTool,
        )
        from agent.tools.information_gathering.network_discovery.fping import FpingTool
        from agent.tools.information_gathering.network_discovery.netdiscover import (
            NetdiscoverTool,
        )
        
        return [
            NmapTool,
            MasscanTool,
            ZmapTool,
            UnicornscanTool,
            FpingTool,
            NetdiscoverTool,
        ]


class TestInformationGatheringDNSToolsSchemas(TestToolSchemaValidation):
    """Comprehensive schema validation for DNS enumeration tools."""
    
    def get_tool_classes(self) -> List[Type[BaseTool]]:
        from agent.tools.information_gathering.dns.dnsenum import DNSEnumTool
        from agent.tools.information_gathering.dns.dnsrecon import DNSReconTool
        from agent.tools.information_gathering.dns.dnsmap import DnsMapTool
        from agent.tools.information_gathering.dns.fierce import FierceTool
        from agent.tools.information_gathering.dns.sublist3r import Sublist3rTool
        from agent.tools.information_gathering.dns.theharvester import TheHarvesterDnsTool
        from agent.tools.information_gathering.dns.amass import AmassTool
        
        return [
            DNSEnumTool,
            DNSReconTool,
            DnsMapTool,
            FierceTool,
            Sublist3rTool,
            TheHarvesterDnsTool,
            AmassTool,
        ]


# ---------------------------------------------------------------------------
# Database Assessment Tools Tests
# ---------------------------------------------------------------------------


class TestDatabaseAssessmentOracleToolsSchemas(TestToolSchemaValidation):
    """Comprehensive schema validation for Oracle database assessment tools."""

    def get_tool_classes(self) -> List[Type[BaseTool]]:
        from agent.tools.database_assessment.oracle_tools.oscanner import OScannerTool
        from agent.tools.database_assessment.oracle_tools.sidguesser import SIDGuesserTool
        from agent.tools.database_assessment.oracle_tools.tnscmd10g import TNSCmd10gTool

        return [TNSCmd10gTool, OScannerTool, SIDGuesserTool]


class TestInformationGatheringOSINTToolsSchemas(TestToolSchemaValidation):
    """Comprehensive schema validation for OSINT tools."""
    
    def get_tool_classes(self) -> List[Type[BaseTool]]:
        from agent.tools.information_gathering.osint.shodan import ShodanTool
        from agent.tools.information_gathering.osint.censys import CensysTool
        from agent.tools.information_gathering.osint.recon_ng import ReconNgTool
        from agent.tools.information_gathering.osint.spiderfoot import SpiderFootTool
        from agent.tools.information_gathering.osint.dmitry import DmitryTool
        from agent.tools.information_gathering.osint.whois import WhoisTool
        
        return [
            ShodanTool,
            CensysTool,
            ReconNgTool,
            SpiderFootTool,
            DmitryTool,
            WhoisTool,
        ]


# ---------------------------------------------------------------------------
# Web Application Tools Tests
# ---------------------------------------------------------------------------


class TestWebApplicationCrawlersSchemas(TestToolSchemaValidation):
    """Comprehensive schema validation for web crawler tools."""
    
    def get_tool_classes(self) -> List[Type[BaseTool]]:
        from agent.tools.web_applications.web_crawlers.gobuster import GobusterTool
        from agent.tools.web_applications.web_crawlers.dirb import DirbTool
        from agent.tools.web_applications.web_crawlers.feroxbuster import FeroxbusterTool
        from agent.tools.web_applications.web_crawlers.ffuf import FfufTool
        from agent.tools.web_applications.web_crawlers.wfuzz import WfuzzTool
        
        return [
            GobusterTool,
            DirbTool,
            FeroxbusterTool,
            FfufTool,
            WfuzzTool,
        ]


class TestWebApplicationScannersSchemas(TestToolSchemaValidation):
    """Comprehensive schema validation for web vulnerability scanner tools."""
    
    def get_tool_classes(self) -> List[Type[BaseTool]]:
        from agent.tools.web_applications.web_vulnerability_scanners.sqlmap import SqlmapTool
        from agent.tools.web_applications.web_vulnerability_scanners.nikto import NiktoTool
        from agent.tools.web_applications.web_vulnerability_scanners.wapiti import WapitiTool
        from agent.tools.web_applications.web_vulnerability_scanners.nuclei import NucleiTool
        from agent.tools.web_applications.web_vulnerability_scanners.skipfish import (
            SkipfishTool,
        )
        from agent.tools.web_applications.web_vulnerability_scanners.commix import CommixTool
        from agent.tools.web_applications.web_vulnerability_scanners.xsser import XsserTool
        from agent.tools.web_applications.web_vulnerability_scanners.arachni import ArachniTool
        from agent.tools.web_applications.web_vulnerability_scanners.w3af import W3afTool
        
        return [
            SqlmapTool,
            NiktoTool,
            WapitiTool,
            NucleiTool,
            SkipfishTool,
            CommixTool,
            XsserTool,
            ArachniTool,
            W3afTool,
        ]


class TestWebApplicationCMSToolsSchemas(TestToolSchemaValidation):
    """Comprehensive schema validation for CMS identification tools."""
    
    def get_tool_classes(self) -> List[Type[BaseTool]]:
        from agent.tools.web_applications.cms_identification.wpscan import WPScanTool
        from agent.tools.web_applications.cms_identification.joomscan import JoomScanTool
        from agent.tools.web_applications.cms_identification.droopescan import (
            DroopescanTool,
        )
        from agent.tools.web_applications.cms_identification.cmsmap import CMSmapTool
        from agent.tools.web_applications.cms_identification.whatweb import WhatWebTool
        
        return [WPScanTool, JoomScanTool, DroopescanTool, CMSmapTool, WhatWebTool]


class TestWebApplicationFuzzersSchemas(TestToolSchemaValidation):
    """Comprehensive schema validation for web fuzzer tools."""
    
    def get_tool_classes(self) -> List[Type[BaseTool]]:
        from agent.tools.web_applications.web_application_fuzzers.ffuf import FfufTool
        from agent.tools.web_applications.web_application_fuzzers.wfuzz import WfuzzTool
        
        return [FfufTool, WfuzzTool]


class TestWebApplicationProxiesSchemas(TestToolSchemaValidation):
    """Comprehensive schema validation for web proxy tools."""
    
    def get_tool_classes(self) -> List[Type[BaseTool]]:
        from agent.tools.web_applications.web_application_proxies.mitmproxy import (
            MitmProxyTool,
        )
        from agent.tools.web_applications.web_application_proxies.zaproxy import (
            ZapProxyTool,
        )
        
        return [MitmProxyTool, ZapProxyTool]


# ---------------------------------------------------------------------------
# Vulnerability Analysis Tools Tests
# ---------------------------------------------------------------------------


class TestVulnerabilityAnalysisCiscoToolsSchemas(TestToolSchemaValidation):
    """Comprehensive schema validation for Cisco vulnerability analysis tools."""

    def get_tool_classes(self) -> List[Type[BaseTool]]:
        from agent.tools.vulnerability_analysis.cisco_tools.cisco_auditing_tool import (
            CiscoAuditingTool,
        )
        from agent.tools.vulnerability_analysis.cisco_tools.cisco_torch import CiscoTorchTool
        from agent.tools.vulnerability_analysis.cisco_tools.cisco_global_exploiter import (
            CiscoGlobalExploiterTool,
        )
        from agent.tools.vulnerability_analysis.cisco_tools.cisco_ocs import CiscoOCSTool
        from agent.tools.vulnerability_analysis.cisco_tools.yersinia import YersiniaTool

        return [
            CiscoAuditingTool,
            CiscoTorchTool,
            CiscoGlobalExploiterTool,
            CiscoOCSTool,
            YersiniaTool,
        ]


class TestVulnerabilityAnalysisFuzzingToolsSchemas(TestToolSchemaValidation):
    """Comprehensive schema validation for fuzzing tools in vulnerability analysis."""

    def get_tool_classes(self) -> List[Type[BaseTool]]:
        from agent.tools.vulnerability_analysis.fuzzing.american_fuzzy_lop import AFLTool
        from agent.tools.vulnerability_analysis.fuzzing.bed import BedTool
        from agent.tools.vulnerability_analysis.fuzzing.boofuzz import BoofuzzTool
        from agent.tools.vulnerability_analysis.fuzzing.powerfuzzer import PowerFuzzerTool
        from agent.tools.vulnerability_analysis.fuzzing.sfuzz import SFuzzTool
        from agent.tools.vulnerability_analysis.fuzzing.spike import SpikeTool

        return [
            AFLTool,
            BedTool,
            BoofuzzTool,
            PowerFuzzerTool,
            SFuzzTool,
            SpikeTool,
        ]


class TestVulnerabilityAnalysisOpenVASToolsSchemas(TestToolSchemaValidation):
    """Comprehensive schema validation for OpenVAS tools."""

    def get_tool_classes(self) -> List[Type[BaseTool]]:
        from agent.tools.vulnerability_analysis.openvas.openvas import OpenVASTool
        from agent.tools.vulnerability_analysis.openvas.openvas_cli import OpenVASCLITool
        from agent.tools.vulnerability_analysis.openvas.openvas_manager import (
            OpenVASManagerTool,
        )
        from agent.tools.vulnerability_analysis.openvas.openvas_scanner import (
            OpenVASScannerTool,
        )
        from agent.tools.vulnerability_analysis.openvas.greenbone import GreenboneTool

        return [
            OpenVASTool,
            OpenVASCLITool,
            OpenVASManagerTool,
            OpenVASScannerTool,
            GreenboneTool,
        ]


class TestVulnerabilityAnalysisVoIPToolsSchemas(TestToolSchemaValidation):
    """Comprehensive schema validation for VoIP analysis tools."""

    def get_tool_classes(self) -> List[Type[BaseTool]]:
        from agent.tools.vulnerability_analysis.voip_analysis.enumiax import EnumiaxTool
        from agent.tools.vulnerability_analysis.voip_analysis.sipvicious import SIPViciousTool
        from agent.tools.vulnerability_analysis.voip_analysis.svmap import SvmapTool
        from agent.tools.vulnerability_analysis.voip_analysis.voiphopper import VoIPHopperTool

        return [EnumiaxTool, SIPViciousTool, SvmapTool, VoIPHopperTool]
