"""Test enhanced tool metadata system."""

import pytest


def test_enhanced_metadata_registry():
    """Test that enhanced metadata is registered and retrievable."""
    from agent.tools.enhanced_tool_metadata import get_enhanced_metadata
    
    # Test nmap metadata
    nmap_metadata = get_enhanced_metadata("information_gathering.network_discovery.nmap")
    assert nmap_metadata is not None
    assert nmap_metadata.tool_id == "information_gathering.network_discovery.nmap"
    assert nmap_metadata.purpose == "Nmap (network_discovery)"
    capability_names = {cap.name for cap in nmap_metadata.capabilities}
    assert {"port_discovery", "service_detection", "os_detection"} <= capability_names
    assert len(nmap_metadata.critical_notes) > 0


def test_rich_tool_description():
    """Test that rich tool descriptions are generated correctly."""
    from agent.tools.enhanced_tool_metadata import build_rich_tool_description
    
    description = build_rich_tool_description("information_gathering.network_discovery.nmap")
    
    # Should contain key information
    assert "port_discovery" in description
    assert "Scan TCP/UDP ports" in description
    assert "service_detection" in description
    assert "os_detection" in description
    assert "CRITICAL NOTES" in description
    assert "Target protocols: tcp, udp" in description


def test_rich_tool_catalog():
    """Test that rich tool catalog is generated for multiple tools."""
    from agent.tools.enhanced_tool_metadata import build_rich_tool_catalog
    
    tools = [
        "information_gathering.network_discovery.nmap",
        "information_gathering.network_discovery.masscan",
    ]
    
    catalog = build_rich_tool_catalog(tools)
    
    # Should contain both tools
    assert "nmap" in catalog
    assert "masscan" in catalog
    
    # Should contain usage guidance
    assert "port_discovery" in catalog
    assert "fast_port_discovery" in catalog
    assert "When to use" in catalog
    
    # Should contain critical notes
    assert "CRITICAL" in catalog


def test_nmap_port_discovery_guidance():
    """Test that nmap metadata clearly explains port discovery usage."""
    from agent.tools.enhanced_tool_metadata import get_enhanced_metadata
    
    nmap_metadata = get_enhanced_metadata("information_gathering.network_discovery.nmap")
    
    # Find primary selector-grade capability
    port_discovery = next(
        (cap for cap in nmap_metadata.capabilities if cap.name == "port_discovery"),
        None
    )
    
    assert port_discovery is not None
    assert "Scan TCP/UDP ports" in port_discovery.description
    assert "returns open ports" in port_discovery.description
    assert "prefer for normal targeted scans" in port_discovery.description


def test_nmap_supporting_capability_guidance():
    """Test that nmap metadata preserves service and OS detection capabilities."""
    from agent.tools.enhanced_tool_metadata import get_enhanced_metadata
    
    nmap_metadata = get_enhanced_metadata("information_gathering.network_discovery.nmap")
    
    capabilities = {cap.name: cap for cap in nmap_metadata.capabilities}

    assert capabilities["service_detection"].description == "Identify running services"
    assert capabilities["os_detection"].description == "Detect operating system"


def test_fallback_for_unknown_tool():
    """Test that unknown tools fall back gracefully."""
    from agent.tools.enhanced_tool_metadata import build_rich_tool_description
    
    description = build_rich_tool_description("nonexistent.tool")
    
    # Should return something (fallback)
    assert "nonexistent.tool" in description


def test_build_tool_catalog_entries_returns_compact_descriptions():
    """Catalog entries expose compact one-line descriptions for planner prompts."""
    from agent.tools.enhanced_tool_metadata import build_tool_catalog_entries

    tool_id = "information_gathering.network_discovery.nmap"
    entries = build_tool_catalog_entries([tool_id])

    assert len(entries) == 1
    entry = entries[0]
    assert set(entry.keys()) == {"id", "name", "category", "description"}
    assert entry["id"] == tool_id
    assert entry["name"] == "nmap"
    assert entry["category"] == "information_gathering"
    assert entry["description"]
    assert "\n" not in entry["description"]
    assert "Capabilities:" not in entry["description"]


def test_build_tool_catalog_entries_falls_back_to_basic_metadata(monkeypatch):
    """Catalog entries fall back to basic metadata when enhanced is unavailable."""
    from agent.tools import enhanced_tool_metadata

    tool_id = "example.tool"
    monkeypatch.setattr(enhanced_tool_metadata, "get_enhanced_metadata", lambda _tool_id: None)
    monkeypatch.setattr(
        "agent.tools.tool_registry.get_tool_metadata",
        lambda _tool_id: {"name": "Example Tool", "description": "Basic fallback description"},
    )

    entries = enhanced_tool_metadata.build_tool_catalog_entries([tool_id])
    assert entries == [
        {
            "id": tool_id,
            "name": "Example Tool",
            "category": "example",
            "description": "Basic fallback description",
        }
    ]


PHASE_1_TOOL_IDS = [
    "information_gathering.web_enumeration.http_request",
    "information_gathering.web_enumeration.http_download",
    "web_applications.web_crawlers.ffuf",
    "web_applications.web_crawlers.gobuster",
    "web_applications.web_crawlers.dirb",
    "web_applications.web_crawlers.wfuzz",
    "web_applications.web_application_fuzzers.ffuf",
    "web_applications.web_application_fuzzers.wfuzz",
    "web_applications.web_application_fuzzers.clusterd",
    "web_applications.web_application_fuzzers.websploit",
    "web_applications.cms_identification.whatweb",
    "web_applications.cms_identification.wpscan",
    "web_applications.cms_identification.droopescan",
    "web_applications.cms_identification.joomscan",
    "web_applications.cms_identification.cmsmap",
]

PHASE_1_VERB_PATTERN = (
    "Fetch", "Download", "Discover", "Scan", "Fingerprint",
    "Enumerate", "Fuzz", "Detect", "Run",
)

PHASE_1_ANTI_PATTERNS = (
    "advanced controls",
    "secure defaults",
    "accurate upstream semantics",
)


@pytest.mark.parametrize("tool_id", PHASE_1_TOOL_IDS)
def test_phase_1_catalog_descriptions_meet_runbook_bar(tool_id):
    """Each Phase 1 tool exposes a contrastive, length-bounded catalog description."""
    from agent.tools.enhanced_tool_metadata import build_tool_catalog_entries
    from core.prompts.builders.tool_planning import _format_catalog

    entries = build_tool_catalog_entries([tool_id])
    assert len(entries) == 1
    description = entries[0]["description"]

    assert description, f"{tool_id} has empty catalog description"
    assert len(description) <= 200
    assert ". " not in description, (
        f"{tool_id} description contains '. ' — _compact_tool_description "
        "will silently drop later capabilities"
    )
    assert any(description.startswith(verb) for verb in PHASE_1_VERB_PATTERN), (
        f"{tool_id} description should start with a runbook-allowed verb: {description!r}"
    )
    for anti in PHASE_1_ANTI_PATTERNS:
        assert anti not in description.lower(), (
            f"{tool_id} description contains anti-pattern phrase {anti!r}"
        )

    rendered = _format_catalog(entries)
    rendered_line = rendered.splitlines()[0]
    assert tool_id in rendered_line
    rendered_description = rendered_line.split(": ", 1)[1]
    assert len(rendered_description) <= 200


def test_phase_1_clusterd_websploit_have_enhanced_metadata():
    """Tools previously missing metadata are now registered."""
    from agent.tools.enhanced_metadata_registry import get_enhanced_tool_metadata

    for tool_id in (
        "web_applications.web_application_fuzzers.clusterd",
        "web_applications.web_application_fuzzers.websploit",
    ):
        meta = get_enhanced_tool_metadata(tool_id)
        assert meta is not None, f"{tool_id} missing enhanced metadata"
        assert meta.capabilities, f"{tool_id} has no capabilities"


def test_phase_1_contrastive_pairs_carry_boundaries():
    """Boundary phrases ('not for ...' / 'use for ...') appear on selection-critical tools."""
    from agent.tools.enhanced_tool_metadata import build_tool_catalog_entries

    pairs = [
        ("information_gathering.web_enumeration.http_request",
         "web_applications.web_crawlers.ffuf"),
        ("web_applications.web_crawlers.ffuf",
         "web_applications.web_application_fuzzers.ffuf"),
        ("web_applications.cms_identification.whatweb",
         "web_applications.web_crawlers.ffuf"),
    ]
    for left, right in pairs:
        descs = {
            entry["id"]: entry["description"]
            for entry in build_tool_catalog_entries([left, right])
        }
        joined = " | ".join(descs.values()).lower()
        assert "not for" in joined or "use for" in joined, (
            f"Pair {left} vs {right} lacks boundary phrasing: {descs}"
        )


PHASE_2_TOOL_IDS = [
    "information_gathering.network_discovery.nmap",
    "information_gathering.network_discovery.masscan",
    "information_gathering.network_discovery.fping",
    "information_gathering.network_discovery.unicornscan",
    "information_gathering.network_discovery.netdiscover",
    "information_gathering.network_discovery.zmap",
    "information_gathering.dns.amass",
    "information_gathering.dns.dnsrecon",
    "information_gathering.dns.dnsenum",
    "information_gathering.dns.dnsmap",
    "information_gathering.dns.fierce",
    "information_gathering.dns.sublist3r",
    "information_gathering.dns.theharvester",
    "information_gathering.route_analysis.traceroute",
    "information_gathering.route_analysis.mtr",
    "information_gathering.route_analysis.pathping",
    "information_gathering.route_analysis.tcptraceroute",
    "information_gathering.osint.censys",
    "information_gathering.osint.dmitry",
    "information_gathering.osint.ike_scan",
    "information_gathering.osint.recon_ng",
    "information_gathering.osint.shodan",
    "information_gathering.osint.spiderfoot",
    "information_gathering.osint.theharvester",
    "information_gathering.osint.whois",
]

PHASE_2_VERB_PATTERN = PHASE_1_VERB_PATTERN + ("Trace", "Lookup")


@pytest.mark.parametrize("tool_id", PHASE_2_TOOL_IDS)
def test_phase_2_catalog_descriptions_meet_runbook_bar(tool_id):
    """Each Phase 2 tool exposes a contrastive, length-bounded catalog description."""
    from agent.tools.enhanced_tool_metadata import build_tool_catalog_entries
    from core.prompts.builders.tool_planning import _format_catalog

    entries = build_tool_catalog_entries([tool_id])
    assert len(entries) == 1
    description = entries[0]["description"]

    assert description, f"{tool_id} has empty catalog description"
    assert len(description) <= 200
    assert ". " not in description, (
        f"{tool_id} description contains '. ' — _compact_tool_description "
        "will silently drop later capabilities"
    )
    assert any(description.startswith(verb) for verb in PHASE_2_VERB_PATTERN), (
        f"{tool_id} description should start with a runbook-allowed verb: {description!r}"
    )
    for anti in PHASE_1_ANTI_PATTERNS:
        assert anti not in description.lower(), (
            f"{tool_id} description contains anti-pattern phrase {anti!r}"
        )

    rendered = _format_catalog(entries)
    rendered_line = rendered.splitlines()[0]
    assert tool_id in rendered_line
    rendered_description = rendered_line.split(": ", 1)[1]
    assert len(rendered_description) <= 200


def test_phase_2_recon_ng_dns_theharvester_have_enhanced_metadata():
    """Tools previously missing metadata are now registered."""
    from agent.tools.enhanced_metadata_registry import get_enhanced_tool_metadata

    for tool_id in (
        "information_gathering.osint.recon_ng",
        "information_gathering.dns.theharvester",
    ):
        meta = get_enhanced_tool_metadata(tool_id)
        assert meta is not None, f"{tool_id} missing enhanced metadata"
        assert meta.capabilities, f"{tool_id} has no capabilities"


def test_phase_2_catalog_descriptions_avoid_forced_ordering():
    """Phase 2 catalog text should avoid rigid before/after sequencing."""
    from agent.tools.enhanced_tool_metadata import build_tool_catalog_entries

    entries = build_tool_catalog_entries(PHASE_2_TOOL_IDS)

    before_clause = "use " + "before"
    after_clause = "use " + "after"
    for entry in entries:
        description = entry["description"].lower()
        assert before_clause not in description
        assert after_clause not in description


def test_network_discovery_overlap_descriptions_explain_preference():
    """Overlapping network tools should expose preference boundaries."""
    from agent.tools.enhanced_tool_metadata import build_tool_catalog_entries

    descriptions = {
        entry["id"]: entry["description"].lower()
        for entry in build_tool_catalog_entries([
            "information_gathering.network_discovery.nmap",
            "information_gathering.network_discovery.masscan",
            "information_gathering.network_discovery.fping",
            "information_gathering.network_discovery.zmap",
        ])
    }

    assert "prefer for normal targeted scans" in descriptions[
        "information_gathering.network_discovery.nmap"
    ]
    assert "prefer for large/full-port sweeps" in descriptions[
        "information_gathering.network_discovery.masscan"
    ]
    assert "not for service or os detection" in descriptions[
        "information_gathering.network_discovery.masscan"
    ]
    assert "prefer for liveness checks" in descriptions[
        "information_gathering.network_discovery.fping"
    ]
    assert "not for port discovery" in descriptions[
        "information_gathering.network_discovery.fping"
    ]
    assert "prefer for internet-scale single-port surveys" in descriptions[
        "information_gathering.network_discovery.zmap"
    ]


def test_phase_2_contrastive_pairs_carry_boundaries():
    """Boundary phrases ('not for ...' / 'use for ...') appear on selection-critical tools."""
    from agent.tools.enhanced_tool_metadata import build_tool_catalog_entries

    pairs = [
        ("information_gathering.network_discovery.masscan",
         "information_gathering.network_discovery.nmap"),
        ("information_gathering.dns.amass",
         "information_gathering.dns.sublist3r"),
        ("information_gathering.route_analysis.traceroute",
         "information_gathering.route_analysis.mtr"),
        ("information_gathering.osint.shodan",
         "information_gathering.osint.censys"),
        ("information_gathering.dns.theharvester",
         "information_gathering.osint.theharvester"),
    ]
    for left, right in pairs:
        descs = {
            entry["id"]: entry["description"]
            for entry in build_tool_catalog_entries([left, right])
        }
        joined = " | ".join(descs.values()).lower()
        assert "not for" in joined or "use for" in joined, (
            f"Pair {left} vs {right} lacks boundary phrasing: {descs}"
        )


PHASE_3_TOOL_IDS = [
    "web_applications.web_vulnerability_scanners.commix",
    "web_applications.web_vulnerability_scanners.nuclei",
    "web_applications.web_vulnerability_scanners.skipfish",
    "web_applications.web_vulnerability_scanners.sqlmap",
    "web_applications.web_vulnerability_scanners.wapiti",
    "web_applications.web_vulnerability_scanners.xsser",
    "web_applications.web_vulnerability_scanners.arachni",
    "web_applications.web_vulnerability_scanners.w3af",
    "vulnerability_analysis.cisco_tools.cisco_auditing_tool",
    "vulnerability_analysis.cisco_tools.cisco_global_exploiter",
    "vulnerability_analysis.cisco_tools.cisco_ocs",
    "vulnerability_analysis.cisco_tools.cisco_torch",
    "vulnerability_analysis.cisco_tools.yersinia",
    "vulnerability_analysis.fuzzing.american_fuzzy_lop",
    "vulnerability_analysis.fuzzing.bed",
    "vulnerability_analysis.fuzzing.boofuzz",
    "vulnerability_analysis.fuzzing.peach",
    "vulnerability_analysis.fuzzing.powerfuzzer",
    "vulnerability_analysis.fuzzing.sfuzz",
    "vulnerability_analysis.fuzzing.spike",
    "vulnerability_analysis.voip_analysis.enumiax",
    "vulnerability_analysis.voip_analysis.sipvicious",
    "vulnerability_analysis.voip_analysis.svmap",
    "vulnerability_analysis.voip_analysis.voiphopper",
]


@pytest.mark.parametrize("tool_id", PHASE_3_TOOL_IDS)
def test_phase_3_catalog_descriptions_meet_runbook_bar(tool_id):
    """Each Phase 3 tool exposes a contrastive, length-bounded catalog description."""
    from agent.tools.enhanced_tool_metadata import build_tool_catalog_entries
    from core.prompts.builders.tool_planning import _format_catalog

    entries = build_tool_catalog_entries([tool_id])
    assert len(entries) == 1
    description = entries[0]["description"]

    assert description, f"{tool_id} has empty catalog description"
    assert len(description) <= 200
    assert ". " not in description, (
        f"{tool_id} description contains '. ' — _compact_tool_description "
        "will silently drop later capabilities"
    )
    assert any(description.startswith(verb) for verb in PHASE_2_VERB_PATTERN), (
        f"{tool_id} description should start with a runbook-allowed verb: {description!r}"
    )
    for anti in PHASE_1_ANTI_PATTERNS:
        assert anti not in description.lower(), (
            f"{tool_id} description contains anti-pattern phrase {anti!r}"
        )

    rendered = _format_catalog(entries)
    rendered_line = rendered.splitlines()[0]
    assert tool_id in rendered_line
    rendered_description = rendered_line.split(": ", 1)[1]
    assert len(rendered_description) <= 200


def test_phase_3_arachni_w3af_peach_have_enhanced_metadata():
    """Tools previously missing metadata are now registered."""
    from agent.tools.enhanced_metadata_registry import get_enhanced_tool_metadata

    for tool_id in (
        "web_applications.web_vulnerability_scanners.arachni",
        "web_applications.web_vulnerability_scanners.w3af",
        "vulnerability_analysis.fuzzing.peach",
    ):
        meta = get_enhanced_tool_metadata(tool_id)
        assert meta is not None, f"{tool_id} missing enhanced metadata"
        assert meta.capabilities, f"{tool_id} has no capabilities"


def test_phase_3_contrastive_pairs_carry_boundaries():
    """Boundary phrases ('not for ...' / 'use for ...') appear on selection-critical tools."""
    from agent.tools.enhanced_tool_metadata import build_tool_catalog_entries

    pairs = [
        ("web_applications.web_vulnerability_scanners.nuclei",
         "web_applications.web_vulnerability_scanners.wapiti"),
        ("web_applications.web_vulnerability_scanners.sqlmap",
         "web_applications.web_vulnerability_scanners.commix"),
        ("web_applications.web_vulnerability_scanners.xsser",
         "web_applications.web_vulnerability_scanners.nuclei"),
        ("web_applications.web_vulnerability_scanners.arachni",
         "web_applications.web_vulnerability_scanners.w3af"),
        ("vulnerability_analysis.fuzzing.american_fuzzy_lop",
         "vulnerability_analysis.fuzzing.boofuzz"),
        ("vulnerability_analysis.cisco_tools.cisco_torch",
         "vulnerability_analysis.cisco_tools.cisco_auditing_tool"),
        ("vulnerability_analysis.voip_analysis.sipvicious",
         "vulnerability_analysis.voip_analysis.svmap"),
    ]
    for left, right in pairs:
        descs = {
            entry["id"]: entry["description"]
            for entry in build_tool_catalog_entries([left, right])
        }
        joined = " | ".join(descs.values()).lower()
        assert "not for" in joined or "use for" in joined, (
            f"Pair {left} vs {right} lacks boundary phrasing: {descs}"
        )


if __name__ == "__main__":
    # Run tests manually
    pytest.main([__file__, "-v"])
