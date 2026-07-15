"""Contract tests for nmap rich metadata extraction.

Validates that the bounded rich metadata contract defined in nmap_semantics.py
produces deterministic, normalized output from realistic nmap XML fixtures.
Tests cover both new rich fields and preservation of existing legacy fields.
"""

import asyncio
import copy
import hashlib
import json
import logging
from types import SimpleNamespace

import pytest
from runtime_shared.semantic.canonical_keys import build_finding_vulnerability_key
from agent.semantic.enrichment import validate_semantic_evidence_entries

from agent.tools.information_gathering.network_discovery.nmap import NmapArgs, NmapTool, parse_nmap_xml
from agent.tools.information_gathering.network_discovery.nmap_semantics import (
    NMAP_CAPABILITY_FAMILY,
    NMAP_SEMANTIC_SCHEMA_VERSION,
    MAX_OS_MATCHES,
    MAX_HOST_SCRIPTS,
    MAX_PORT_SCRIPTS,
    MAX_TRACE_HOPS,
    MAX_SCRIPT_SUMMARY_LEN,
    enrich_host,
    enrich_port,
    build_host_profiled_observation,
    build_service_profiled_observation,
    build_nmap_semantic_evidence,
    classify_script_findings,
    _parse_hostnames,
    _parse_os_matches,
    _parse_script_summaries,
    _parse_trace_hops,
    _build_service_profile,
    _truncate,
)
import xml.etree.ElementTree as ET


# ---------------------------------------------------------------------------
# Rich XML fixture — realistic but compact
# ---------------------------------------------------------------------------

RICH_XML = """\
<?xml version='1.0'?>
<nmaprun>
  <runstats>
    <hosts up='1' down='0' total='1'/>
  </runstats>
  <host>
    <address addr='10.0.0.5' addrtype='ipv4'/>
    <status state='up'/>
    <hostnames>
      <hostname name='webserver.local' type='PTR'/>
      <hostname name='web.example.com' type='user'/>
    </hostnames>
    <os>
      <osmatch name='Linux 5.4' accuracy='98'/>
      <osmatch name='Linux 5.10' accuracy='95'/>
      <osmatch name='FreeBSD 12.2' accuracy='80'/>
      <osmatch name='Linux 4.19' accuracy='75'/>
    </os>
    <script id='ssh-hostkey' output='2048 aa:bb:cc:dd RSA\\n256 ee:ff:00:11 ECDSA'/>
    <script id='smb-os-discovery' output='OS: Windows Server 2019'/>
    <ports>
      <port protocol='tcp' portid='22'>
        <state state='open'/>
        <service name='ssh' product='OpenSSH' version='8.9p1'/>
        <script id='ssh-hostkey' output='2048 aa:bb:cc:dd RSA'/>
      </port>
      <port protocol='tcp' portid='80'>
        <state state='open'/>
        <service name='http' product='nginx' version='1.25.2'/>
        <script id='http-title' output='Welcome to Example'/>
        <script id='http-server-header' output='nginx/1.25.2'/>
        <script id='http-methods' output='GET HEAD POST OPTIONS'/>
      </port>
      <port protocol='tcp' portid='443'>
        <state state='open'/>
        <service name='https' product='Apache httpd' version='2.4.6'/>
        <script id='ssl-cert' output='Subject: CN=example.com; Not valid after: 2025-01-01T00:00:00 - expired'/>
        <script id='http-title' output='Secure Portal'/>
      </port>
      <port protocol='tcp' portid='21'>
        <state state='open'/>
        <service name='ftp' product='vsftpd' version='3.0.3'/>
        <script id='ftp-anon' output='Anonymous FTP login allowed (FTP code 230)'/>
      </port>
      <port protocol='tcp' portid='445'>
        <state state='open'/>
        <service name='microsoft-ds'/>
        <script id='smb-security-mode' output='Message signing is disabled'/>
      </port>
      <port protocol='tcp' portid='8080'>
        <state state='closed'/>
        <service name='http-proxy'/>
      </port>
    </ports>
    <trace>
      <hop ttl='1' ipaddr='10.0.0.1' rtt='0.34' host='gateway.local'/>
      <hop ttl='2' ipaddr='10.0.1.1' rtt='1.23'/>
      <hop ttl='3' ipaddr='10.0.0.5' rtt='2.01' host='webserver.local'/>
    </trace>
  </host>
</nmaprun>
"""

# Minimal XML with no rich data — tests graceful degradation
MINIMAL_XML = """\
<?xml version='1.0'?>
<nmaprun>
  <runstats>
    <hosts up='1' down='0' total='1'/>
  </runstats>
  <host>
    <address addr='192.168.1.1' addrtype='ipv4'/>
    <status state='up'/>
    <ports>
      <port protocol='tcp' portid='80'>
        <state state='open'/>
        <service name='http' product='nginx' version='1.18.0'/>
      </port>
    </ports>
  </host>
</nmaprun>
"""

# XML with malformed/partial rich data — tests safe degradation
PARTIAL_XML = """\
<?xml version='1.0'?>
<nmaprun>
  <runstats>
    <hosts up='1' down='0' total='1'/>
  </runstats>
  <host>
    <address addr='10.0.0.10' addrtype='ipv4'/>
    <status state='up'/>
    <os>
      <osmatch name='' accuracy='90'/>
      <osmatch name='Linux 5.x' accuracy='notanumber'/>
      <osmatch name='Valid OS' accuracy='85'/>
    </os>
    <script id='' output='bad script with no id'/>
    <script id='valid-script' output=''/>
    <ports>
      <port protocol='tcp' portid='80'>
        <state state='open'/>
        <service name='http'/>
        <script id='http-title' output=''/>
      </port>
    </ports>
    <trace>
      <hop ttl='notanumber' ipaddr='10.0.0.1' rtt='0.5'/>
      <hop ttl='1' ipaddr='' rtt='0.5'/>
      <hop ttl='2' ipaddr='10.0.0.10' rtt='badrtt'/>
    </trace>
  </host>
</nmaprun>
"""


# ---------------------------------------------------------------------------
# Tests: Legacy field preservation
# ---------------------------------------------------------------------------

class TestParseNmapXmlIntegration:
    """Verify parse_nmap_xml() produces enriched metadata end-to-end."""

    def test_rich_xml_produces_host_enrichment(self):
        metadata = parse_nmap_xml(RICH_XML)
        host = metadata["hosts"][0]
        assert host["hostnames"] == ["web.example.com", "webserver.local"]
        assert host["os_top_guess"] == "Linux 5.4"
        assert len(host["os_matches"]) <= MAX_OS_MATCHES
        assert len(host["host_scripts"]) > 0
        assert len(host["trace_hops"]) == 3

    def test_rich_xml_produces_port_enrichment(self):
        metadata = parse_nmap_xml(RICH_XML)
        host = metadata["hosts"][0]
        by_port = {p["port"]: p for p in host["ports"]}
        # Port 80 should have service_profile with http_title
        assert by_port[80]["service_profile"]["http_title"] == "Welcome to Example"
        assert by_port[80]["service_profile"]["server_header"] == "nginx/1.25.2"
        # Port 22 should have ssh-hostkey script
        assert by_port[22]["service_profile"]["script_summaries"][0]["script_id"] == "ssh-hostkey"

    def test_minimal_xml_produces_no_enrichment(self):
        metadata = parse_nmap_xml(MINIMAL_XML)
        host = metadata["hosts"][0]
        assert "hostnames" not in host
        assert "os_matches" not in host
        assert "service_profile" not in host["ports"][0]

    def test_flat_open_ports_also_get_enriched(self):
        """The flat open_ports list should include service_profile when present."""
        metadata = parse_nmap_xml(RICH_XML)
        by_port = {p["port"]: p for p in metadata["open_ports"]}
        assert "service_profile" in by_port[80]


class TestLegacyFieldPreservation:
    """Verify that existing metadata keys are unchanged after rich parsing."""

    def test_legacy_keys_present_in_rich_xml(self):
        metadata = parse_nmap_xml(RICH_XML)
        assert "open_ports" in metadata
        assert "hosts_up" in metadata
        assert "hosts_total" in metadata
        assert "hosts" in metadata
        assert "host_status" in metadata

    def test_legacy_host_counts(self):
        metadata = parse_nmap_xml(RICH_XML)
        assert metadata["hosts_up"] == 1
        assert metadata["hosts_total"] == 1

    def test_legacy_open_ports_flat_list(self):
        metadata = parse_nmap_xml(RICH_XML)
        # Only open ports should appear (port 8080 is closed)
        ports = {p["port"] for p in metadata["open_ports"]}
        assert 8080 not in ports
        assert 80 in ports
        assert 22 in ports

    def test_legacy_host_ip(self):
        metadata = parse_nmap_xml(RICH_XML)
        assert metadata["hosts"][0]["ip"] == "10.0.0.5"

    def test_legacy_port_service_product_version(self):
        metadata = parse_nmap_xml(RICH_XML)
        host = metadata["hosts"][0]
        by_port = {p["port"]: p for p in host["ports"]}
        assert by_port[80]["service"] == "http"
        assert by_port[80]["product"] == "nginx"
        assert by_port[80]["version"] == "1.25.2"

    def test_legacy_keys_in_minimal_xml(self):
        """Minimal XML without rich data still produces all legacy keys."""
        metadata = parse_nmap_xml(MINIMAL_XML)
        assert metadata["hosts_up"] == 1
        assert metadata["hosts_total"] == 1
        assert len(metadata["open_ports"]) == 1
        assert metadata["hosts"][0]["ip"] == "192.168.1.1"


# ---------------------------------------------------------------------------
# Tests: Host-level rich metadata
# ---------------------------------------------------------------------------

class TestHostRichMetadata:
    """Verify host-level rich field extraction from XML."""

    def _get_enriched_host(self, xml_text: str = RICH_XML) -> dict:
        root = ET.fromstring(xml_text)
        host_el = root.find("host")
        host_info = {"ip": "10.0.0.5", "status": "up", "ports": []}
        enrich_host(host_el, host_info)
        return host_info

    def test_hostnames_sorted(self):
        host = self._get_enriched_host()
        assert host["hostnames"] == ["web.example.com", "webserver.local"]

    def test_os_matches_bounded_and_sorted(self):
        host = self._get_enriched_host()
        assert len(host["os_matches"]) <= MAX_OS_MATCHES
        # Highest accuracy first
        assert host["os_matches"][0]["name"] == "Linux 5.4"
        assert host["os_matches"][0]["accuracy"] == 98

    def test_os_top_guess(self):
        host = self._get_enriched_host()
        assert host["os_top_guess"] == "Linux 5.4"

    def test_host_scripts_sorted_by_id(self):
        host = self._get_enriched_host()
        script_ids = [s["script_id"] for s in host["host_scripts"]]
        assert script_ids == sorted(script_ids)

    def test_trace_hops_sorted_by_ttl(self):
        host = self._get_enriched_host()
        ttls = [h["ttl"] for h in host["trace_hops"]]
        assert ttls == sorted(ttls)
        assert len(host["trace_hops"]) == 3

    def test_trace_hop_fields(self):
        host = self._get_enriched_host()
        hop1 = host["trace_hops"][0]
        assert hop1["ttl"] == 1
        assert hop1["ip"] == "10.0.0.1"
        assert hop1["rtt_ms"] == 0.34
        assert hop1["host"] == "gateway.local"
        # Hop 2 has no host
        hop2 = host["trace_hops"][1]
        assert hop2["host"] is None

    def test_no_rich_fields_on_minimal_xml(self):
        """Minimal XML without rich data should not add optional fields."""
        root = ET.fromstring(MINIMAL_XML)
        host_el = root.find("host")
        host_info = {"ip": "192.168.1.1", "status": "up", "ports": []}
        enrich_host(host_el, host_info)
        assert "hostnames" not in host_info
        assert "os_matches" not in host_info
        assert "os_top_guess" not in host_info
        assert "host_scripts" not in host_info
        assert "trace_hops" not in host_info


# ---------------------------------------------------------------------------
# Tests: Port/service-level rich metadata
# ---------------------------------------------------------------------------

class TestPortRichMetadata:
    """Verify port-level rich service profile extraction."""

    def _get_enriched_ports(self) -> dict:
        root = ET.fromstring(RICH_XML)
        host_el = root.find("host")
        by_port: dict[int, dict] = {}
        for port_el in host_el.findall("ports/port"):
            state_el = port_el.find("state")
            if state_el is not None and state_el.attrib.get("state") == "open":
                port_info = {
                    "port": int(port_el.attrib.get("portid", 0)),
                    "protocol": port_el.attrib.get("protocol"),
                    "service": port_el.find("service").attrib.get("name") if port_el.find("service") is not None else None,
                    "product": port_el.find("service").attrib.get("product") if port_el.find("service") is not None else None,
                    "version": port_el.find("service").attrib.get("version") if port_el.find("service") is not None else None,
                }
                enrich_port(port_el, port_info)
                by_port[port_info["port"]] = port_info
        return by_port

    def test_http_port_has_service_profile(self):
        ports = self._get_enriched_ports()
        profile = ports[80]["service_profile"]
        assert profile is not None
        assert profile["http_title"] == "Welcome to Example"
        assert profile["server_header"] == "nginx/1.25.2"

    def test_http_port_script_summaries_sorted(self):
        ports = self._get_enriched_ports()
        profile = ports[80]["service_profile"]
        script_ids = [s["script_id"] for s in profile["script_summaries"]]
        assert script_ids == sorted(script_ids)

    def test_ssh_port_has_service_profile(self):
        ports = self._get_enriched_ports()
        profile = ports[22]["service_profile"]
        assert profile is not None
        assert len(profile["script_summaries"]) == 1
        assert profile["script_summaries"][0]["script_id"] == "ssh-hostkey"

    def test_port_without_scripts_has_no_profile(self):
        """Port 445 has a script, so it should have a profile.
        A port with no scripts would have no profile."""
        root = ET.fromstring(MINIMAL_XML)
        host_el = root.find("host")
        port_el = host_el.find("ports/port")
        port_info = {"port": 80, "protocol": "tcp", "service": "http"}
        enrich_port(port_el, port_info)
        assert "service_profile" not in port_info

    def test_existing_port_fields_preserved(self):
        ports = self._get_enriched_ports()
        p80 = ports[80]
        assert p80["port"] == 80
        assert p80["protocol"] == "tcp"
        assert p80["service"] == "http"
        assert p80["product"] == "nginx"
        assert p80["version"] == "1.25.2"


# ---------------------------------------------------------------------------
# Tests: Graceful degradation with partial/malformed data
# ---------------------------------------------------------------------------

class TestGracefulDegradation:
    """Verify safe handling of malformed or partial rich XML sections."""

    def _get_enriched_partial_host(self) -> dict:
        root = ET.fromstring(PARTIAL_XML)
        host_el = root.find("host")
        host_info = {"ip": "10.0.0.10", "status": "up", "ports": []}
        enrich_host(host_el, host_info)
        return host_info

    def test_empty_name_os_match_skipped(self):
        host = self._get_enriched_partial_host()
        # Empty name is skipped; non-numeric accuracy gets None
        assert len(host["os_matches"]) == 2
        names = [m["name"] for m in host["os_matches"]]
        assert "Valid OS" in names
        assert "Linux 5.x" in names

    def test_non_numeric_accuracy_becomes_none(self):
        host = self._get_enriched_partial_host()
        linux_match = [m for m in host["os_matches"] if m["name"] == "Linux 5.x"][0]
        assert linux_match["accuracy"] is None

    def test_empty_script_id_skipped(self):
        host = self._get_enriched_partial_host()
        # Script with id='' is skipped; valid-script with empty output is kept
        script_ids = [s["script_id"] for s in host["host_scripts"]]
        assert "" not in script_ids
        assert "valid-script" in script_ids

    def test_malformed_trace_hops_skipped(self):
        host = self._get_enriched_partial_host()
        # ttl='notanumber' and ipaddr='' are both skipped
        # Only ttl=2 / ipaddr='10.0.0.10' survives (bad rtt becomes None)
        assert len(host["trace_hops"]) == 1
        hop = host["trace_hops"][0]
        assert hop["ttl"] == 2
        assert hop["ip"] == "10.0.0.10"
        assert hop["rtt_ms"] is None  # 'badrtt' can't parse

    def test_core_host_info_preserved_despite_malformed_rich_data(self):
        """Core fields must survive even when rich sections are partially broken."""
        metadata = parse_nmap_xml(PARTIAL_XML)
        assert metadata["hosts_up"] == 1
        assert len(metadata["hosts"]) == 1
        assert metadata["hosts"][0]["ip"] == "10.0.0.10"


# ---------------------------------------------------------------------------
# Tests: Deterministic ordering and truncation
# ---------------------------------------------------------------------------

class TestDeterministicOrdering:
    """Verify that rich fields are deterministically ordered and bounded."""

    def test_os_matches_sorted_by_accuracy_desc_then_name(self):
        matches = _parse_os_matches(ET.fromstring(
            "<host><os>"
            "<osmatch name='B OS' accuracy='90'/>"
            "<osmatch name='A OS' accuracy='90'/>"
            "<osmatch name='C OS' accuracy='95'/>"
            "</os></host>"
        ))
        assert matches[0]["name"] == "C OS"
        assert matches[1]["name"] == "A OS"
        assert matches[2]["name"] == "B OS"

    def test_script_summaries_sorted_by_id(self):
        summaries = _parse_script_summaries(ET.fromstring(
            "<parent>"
            "<script id='z-script' output='z'/>"
            "<script id='a-script' output='a'/>"
            "<script id='m-script' output='m'/>"
            "</parent>"
        ), max_scripts=10)
        ids = [s["script_id"] for s in summaries]
        assert ids == ["a-script", "m-script", "z-script"]

    def test_truncation_of_long_summary(self):
        long_text = "x" * 500
        result = _truncate(long_text, MAX_SCRIPT_SUMMARY_LEN)
        assert len(result) == MAX_SCRIPT_SUMMARY_LEN
        assert result.endswith("...")

    def test_short_summary_not_truncated(self):
        short_text = "hello"
        assert _truncate(short_text) == "hello"

    def test_os_matches_bounded_to_max(self):
        xml = "<host><os>"
        for i in range(10):
            xml += f"<osmatch name='OS {i}' accuracy='{90 - i}'/>"
        xml += "</os></host>"
        matches = _parse_os_matches(ET.fromstring(xml))
        assert len(matches) == MAX_OS_MATCHES

    def test_trace_hops_bounded_to_max(self):
        xml = "<host><trace>"
        for i in range(50):
            xml += f"<hop ttl='{i+1}' ipaddr='10.0.0.{i % 256}' rtt='1.0'/>"
        xml += "</trace></host>"
        hops = _parse_trace_hops(ET.fromstring(xml))
        assert len(hops) == MAX_TRACE_HOPS


# ---------------------------------------------------------------------------
# Tests: Semantic observation builders
# ---------------------------------------------------------------------------

class TestSemanticObservationBuilders:
    """Verify observation construction from enriched metadata."""

    def test_host_profiled_observation_with_rich_data(self):
        host_info = {
            "ip": "10.0.0.5",
            "status": "up",
            "hostnames": ["web.example.com"],
            "os_top_guess": "Linux 5.4",
            "os_matches": [{"name": "Linux 5.4", "accuracy": 98}],
            "host_scripts": [{"script_id": "ssh-hostkey", "summary": "key data"}],
            "trace_hops": [{"ttl": 1, "ip": "10.0.0.1", "host": None, "rtt_ms": 0.5}],
        }
        obs = build_host_profiled_observation("10.0.0.5", host_info)
        assert obs is not None
        assert obs["observation_type"] == "network.host_profiled"
        assert obs["subject_type"] == "host.ip"
        assert obs["subject_key"] == "host.ip:10.0.0.5"
        assert obs["payload"]["os_top_guess"] == "Linux 5.4"
        assert obs["payload"]["trace_summary"]["hop_count"] == 1

    def test_host_profiled_observation_none_when_no_rich_data(self):
        """A host with only basic status should NOT produce a profiled observation."""
        host_info = {"ip": "10.0.0.1", "status": "up"}
        obs = build_host_profiled_observation("10.0.0.1", host_info)
        assert obs is None

    def test_service_profiled_observation(self):
        port_info = {
            "port": 80,
            "protocol": "tcp",
            "service": "http",
            "product": "nginx",
            "version": "1.25.2",
            "service_profile": {
                "http_title": "Welcome",
                "server_header": "nginx/1.25.2",
                "script_summaries": [{"script_id": "http-title", "summary": "Welcome"}],
            },
        }
        obs = build_service_profiled_observation("10.0.0.5", port_info)
        assert obs is not None
        assert obs["observation_type"] == "network.service_profiled"
        assert obs["subject_key"] == "service.socket:10.0.0.5/tcp/80"
        assert obs["payload"]["http_title"] == "Welcome"

    def test_service_profiled_observation_none_without_profile(self):
        port_info = {"port": 80, "protocol": "tcp", "service": "http"}
        obs = build_service_profiled_observation("10.0.0.5", port_info)
        assert obs is None


# ---------------------------------------------------------------------------
# Tests: Curated findings allowlist
# ---------------------------------------------------------------------------

class TestFindingsAllowlist:
    """Verify that only curated risk-bearing scripts produce findings."""

    def test_ftp_anon_finding(self):
        findings = classify_script_findings(
            "10.0.0.5", 21, "tcp",
            [{"script_id": "ftp-anon", "summary": "Anonymous FTP login allowed (FTP code 230)"}],
        )
        assert len(findings) == 1
        expected_key = build_finding_vulnerability_key(
            subject_key="service.socket:10.0.0.5/tcp/21",
            detector_id="nmap/ftp-anon",
        )
        assert findings[0]["observation_type"] == "finding.vulnerability_detected"
        assert findings[0]["subject_type"] == "finding.vulnerability"
        assert findings[0]["subject_key"] == expected_key
        assert findings[0]["payload"]["detector_id"] == "nmap/ftp-anon"

    def test_smb_signing_disabled_finding(self):
        findings = classify_script_findings(
            "10.0.0.5", 445, "tcp",
            [{"script_id": "smb-security-mode", "summary": "Message signing is disabled"}],
        )
        assert len(findings) == 1
        assert findings[0]["payload"]["detector_id"] == "nmap/smb-signing-disabled"

    def test_ssl_cert_expired_finding(self):
        findings = classify_script_findings(
            "10.0.0.5", 443, "tcp",
            [{"script_id": "ssl-cert", "summary": "CN=example.com; Not valid after: 2025-01-01 - expired"}],
        )
        assert len(findings) == 1
        assert findings[0]["payload"]["detector_id"] == "nmap/ssl-cert-expired"

    def test_non_allowlisted_script_produces_no_finding(self):
        """Generic http-title should NOT become a finding."""
        findings = classify_script_findings(
            "10.0.0.5", 80, "tcp",
            [{"script_id": "http-title", "summary": "Welcome to Example"}],
        )
        assert len(findings) == 0

    def test_ftp_anon_without_allowed_keyword_no_finding(self):
        """ftp-anon script without 'allowed' or 'logged in' should not trigger."""
        findings = classify_script_findings(
            "10.0.0.5", 21, "tcp",
            [{"script_id": "ftp-anon", "summary": "Anonymous FTP login denied"}],
        )
        assert len(findings) == 0

    def test_ssl_cert_valid_no_finding(self):
        """A valid ssl-cert should NOT produce an expired finding."""
        findings = classify_script_findings(
            "10.0.0.5", 443, "tcp",
            [{"script_id": "ssl-cert", "summary": "CN=example.com; Valid until 2030-01-01"}],
        )
        assert len(findings) == 0


# ---------------------------------------------------------------------------
# Tests: Semantic evidence builder (Phase 4.1)
# ---------------------------------------------------------------------------

class TestNmapSemanticEvidenceBuilder:
    """Verify nmap semantic evidence mapping is pure and vocab-conformant."""

    def test_build_nmap_semantic_evidence_is_pure(self):
        metadata = parse_nmap_xml(RICH_XML)
        args = NmapArgs(
            target="10.0.0.5",
            scan_types=["-sS"],
            timing="-T4",
            service_detection=True,
            script_categories=["default", "safe"],
        )
        metadata_before = copy.deepcopy(metadata)
        args_before = args.model_dump()

        _ = build_nmap_semantic_evidence(metadata, args)

        assert metadata == metadata_before
        assert args.model_dump() == args_before

    def test_build_nmap_semantic_evidence_includes_expected_types(self):
        metadata = parse_nmap_xml(RICH_XML)
        args = NmapArgs(
            target="10.0.0.5,10.0.0.6",
            scan_types=["-sS", "-sV"],
            timing="-T4",
            ports="22,80,443",
            script_categories=["default"],
            service_detection=True,
        )

        evidence = build_nmap_semantic_evidence(metadata, args)
        types = {entry["type"] for entry in evidence}

        assert "variant" in types
        assert "execution_parameter" in types
        assert "result_summary" in types
        assert "target_template" in types

        valid_entries, dropped_entries = validate_semantic_evidence_entries(evidence)
        assert len(valid_entries) > 0
        assert dropped_entries == []

    def test_build_nmap_semantic_evidence_masks_hostname_targets(self):
        metadata = parse_nmap_xml(RICH_XML)
        args = NmapArgs(target="scanme.nmap.org,10.0.0.5")

        evidence = build_nmap_semantic_evidence(metadata, args)
        target_sample = next(
            item for item in evidence
            if item.get("type") == "target_template" and item.get("name") == "target_sample"
        )

        assert "scanme.nmap.org" not in str(target_sample["value"])
        assert "<redacted-target>" in str(target_sample["value"])


# ---------------------------------------------------------------------------
# Tests: End-to-end semantic emission via NmapTool
# ---------------------------------------------------------------------------

class TestNmapToolSemanticEmission:
    """Verify NmapTool.emit_semantic_observations() produces complete observation sets."""

    def _emit(self, xml_text: str = RICH_XML):
        tool = NmapTool()
        metadata = parse_nmap_xml(xml_text)
        args = NmapArgs(target="10.0.0.5")
        return tool.emit_semantic_observations(
            stdout=xml_text, stderr="", exit_code=0, args=args, metadata=metadata,
        )

    def test_nmap_emits_vocab_conformant_evidence(self):
        tool = NmapTool()
        args = NmapArgs(
            target="10.0.0.5,scanme.nmap.org",
            scan_types=["-sS", "-sV"],
            timing="-T4",
            ports="22,80,443",
            script_categories=["default"],
            service_detection=True,
        )
        metadata = parse_nmap_xml(RICH_XML)

        evidence = tool.emit_semantic_evidence(
            stdout=RICH_XML,
            stderr="",
            exit_code=0,
            args=args,
            metadata=metadata,
        )
        valid_entries, dropped_entries = validate_semantic_evidence_entries(evidence)

        assert len(valid_entries) > 0
        assert dropped_entries == []

    def test_nmap_emit_semantic_evidence_matches_builder_output(self):
        tool = NmapTool()
        args = NmapArgs(target="10.0.0.5", scan_types=["-sS"], timing="-T4")
        metadata = parse_nmap_xml(RICH_XML)

        emitted = tool.emit_semantic_evidence(
            stdout=RICH_XML,
            stderr="",
            exit_code=0,
            args=args,
            metadata=metadata,
        )
        expected = build_nmap_semantic_evidence(metadata, args)

        assert emitted == expected

    def test_existing_observations_emitted(self):
        observations = self._emit()
        types = {o["observation_type"] for o in observations}
        assert "network.host_discovered" in types
        assert "network.open_port" in types
        assert "network.service_detected" in types

    def test_new_profiled_observations_emitted(self):
        observations = self._emit()
        types = {o["observation_type"] for o in observations}
        assert "network.host_profiled" in types
        assert "network.service_profiled" in types

    def test_curated_findings_emitted(self):
        observations = self._emit()
        findings = [o for o in observations if o["observation_type"] == "finding.vulnerability_detected"]
        detector_ids = {f["payload"]["detector_id"] for f in findings}
        # RICH_XML has ftp-anon, smb-security-mode, and expired ssl-cert
        assert "nmap/ftp-anon" in detector_ids
        assert "nmap/smb-signing-disabled" in detector_ids
        assert "nmap/ssl-cert-expired" in detector_ids

    def test_service_detected_payload_keeps_version_parity_fields(self):
        observations = self._emit()
        service_obs = [
            item for item in observations if item["observation_type"] == "network.service_detected"
        ]
        ssh_service = next(item for item in service_obs if item["subject_key"] == "service.socket:10.0.0.5/tcp/22")
        assert ssh_service["payload"]["product"] == "OpenSSH"
        assert ssh_service["payload"]["version"] == "8.9p1"
        assert ssh_service["payload"]["product_hint"] == "OpenSSH 8.9p1"

    def test_parse_output_places_transport_markers_in_tool_metadata(self):
        tool = NmapTool()
        metadata = tool.parse_output(
            stdout=RICH_XML,
            stderr="",
            exit_code=0,
            args=NmapArgs(target="10.0.0.5"),
        )
        assert metadata["semantic_schema_version"] == NMAP_SEMANTIC_SCHEMA_VERSION
        assert metadata["capability_family"] == NMAP_CAPABILITY_FAMILY

    def test_minimal_xml_emits_only_inventory_observations(self):
        observations = self._emit(MINIMAL_XML)
        types = {o["observation_type"] for o in observations}
        assert "network.host_discovered" in types
        assert "network.open_port" in types
        assert "network.service_detected" in types
        # No profiled observations for minimal XML
        assert "network.host_profiled" not in types
        assert "network.service_profiled" not in types
        assert "finding.vulnerability_detected" not in types

    def test_empty_metadata_returns_empty(self):
        tool = NmapTool()
        args = NmapArgs(target="10.0.0.5")
        observations = tool.emit_semantic_observations(
            stdout="", stderr="", exit_code=1, args=args, metadata={},
        )
        assert observations == []

    def test_host_discovered_subject_key(self):
        observations = self._emit()
        host_obs = [o for o in observations if o["observation_type"] == "network.host_discovered"]
        assert len(host_obs) == 1
        assert host_obs[0]["subject_key"] == "host.ip:10.0.0.5"

    def test_open_port_observation_count(self):
        observations = self._emit()
        open_port_obs = [o for o in observations if o["observation_type"] == "network.open_port"]
        # RICH_XML has 5 open ports (22, 80, 443, 21, 445)
        assert len(open_port_obs) == 5


class _PromptEchoLLM:
    """Deterministic fake LLM that exposes prompt bytes through structured output."""

    model = "test-model"

    def __init__(self) -> None:
        self.last_prompt: str | None = None

    async def chat_with_usage(self, system_prompt: str, user_prompt: str, **kwargs):
        self.last_prompt = user_prompt
        digest = hashlib.sha256(user_prompt.encode("utf-8")).hexdigest()
        return SimpleNamespace(
            content="",
            structured_output={
                "summary": f"prompt-sha256:{digest}",
                "key_findings": [f"prompt-len:{len(user_prompt)}"],
                "structured_signals": [],
                "decision_evidence": [digest[:16]],
                "lossiness_risk": "low",
            },
            usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        )


def test_nmap_prompt_baseline_frozen_v4_with_evidence():
    from agent.context.tool_processor import UniversalToolProcessor

    llm = _PromptEchoLLM()
    processor = UniversalToolProcessor(
        llm_client=llm,
        logger=logging.getLogger("test.nmap.prompt.baseline.pre_v4"),
    )
    tool = NmapTool()
    args = NmapArgs(target="10.0.0.5")

    parsed_metadata = tool.parse_output(stdout=RICH_XML, stderr="", exit_code=0, args=args)
    semantic_observations = tool.emit_semantic_observations(
        stdout=RICH_XML,
        stderr="",
        exit_code=0,
        args=args,
        metadata=parsed_metadata,
    )
    semantic_evidence = tool.emit_semantic_evidence(
        stdout=RICH_XML,
        stderr="",
        exit_code=0,
        args=args,
        metadata=parsed_metadata,
    )
    metadata = {
        "tool_metadata": {
            **parsed_metadata,
            "semantic_observations": semantic_observations,
            "semantic_evidence": semantic_evidence,
        }
    }

    result = asyncio.run(
        processor.process_output(
            "information_gathering.network_discovery.nmap",
            RICH_XML,
            metadata=metadata,
        )
    )

    assert llm.last_prompt is not None
    # Rebaselined 2026-04-21 after centralizing evidence normalization through
    # validate_semantic_evidence_entries so extract_runtime_semantic_inputs no
    # longer bypasses the validator. Previously injected evidence without the
    # canonical detail={} field reached the prompt; the canonical shape does.
    assert len(llm.last_prompt) == 19093
    assert (
        hashlib.sha256(llm.last_prompt.encode("utf-8")).hexdigest()
        == "044cd84160b892cc690414c00bcae77703654053525d0e3f335c1daeb7b5ca28"
    )
    assert "network.host_profiled" in llm.last_prompt
    assert '"result_summary":[' in llm.last_prompt

    assert result.summary == "prompt-sha256:044cd84160b892cc690414c00bcae77703654053525d0e3f335c1daeb7b5ca28"
    assert result.key_findings == ["prompt-len:19093"]
    assert result.structured_signals == []
    assert result.decision_evidence == ["044cd84160b892cc"]
    assert result.lossiness_risk == "low"


def test_nmap_compressor_prompt_includes_result_summary():
    from agent.context.tool_processor import UniversalToolProcessor

    llm = _PromptEchoLLM()
    processor = UniversalToolProcessor(
        llm_client=llm,
        logger=logging.getLogger("test.nmap.prompt.result_summary"),
    )
    tool = NmapTool()
    args = NmapArgs(target="10.0.0.5")

    parsed_metadata = tool.parse_output(stdout=RICH_XML, stderr="", exit_code=0, args=args)
    semantic_observations = tool.emit_semantic_observations(
        stdout=RICH_XML,
        stderr="",
        exit_code=0,
        args=args,
        metadata=parsed_metadata,
    )
    semantic_evidence = tool.emit_semantic_evidence(
        stdout=RICH_XML,
        stderr="",
        exit_code=0,
        args=args,
        metadata=parsed_metadata,
    )

    asyncio.run(
        processor.process_output(
            "information_gathering.network_discovery.nmap",
            RICH_XML,
            metadata={
                "tool_metadata": {
                    **parsed_metadata,
                    "semantic_observations": semantic_observations,
                    "semantic_evidence": semantic_evidence,
                }
            },
        )
    )

    assert llm.last_prompt is not None
    assert '"result_summary":[' in llm.last_prompt
    assert '"name":"hosts_up"' in llm.last_prompt
    assert '"name":"open_ports_count"' in llm.last_prompt


def test_nmap_no_observation_regressions():
    tool = NmapTool()
    metadata = parse_nmap_xml(RICH_XML)
    args = NmapArgs(target="10.0.0.5")
    observations = tool.emit_semantic_observations(
        stdout=RICH_XML,
        stderr="",
        exit_code=0,
        args=args,
        metadata=metadata,
    )
    digest = hashlib.sha256(
        json.dumps(observations, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()

    assert digest == "7e6d94b7b010faecbcd6cfa24a316c728ba1bef4636ab2310fadfcfd1f26a7c4"
