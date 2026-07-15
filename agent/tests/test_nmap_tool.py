import subprocess
from agent.tools import NmapTool, NmapArgs, parse_nmap_xml, validate_and_execute_tool

SAMPLE_XML = """<?xml version='1.0'?>
<nmaprun>
  <runstats>
    <hosts up='1' down='0' total='1'/>
  </runstats>
  <host>
    <address addr='192.168.1.1' addrtype='ipv4'/>
    <status state='up'/>
    <ports>
      <port protocol='tcp' portid='22'>
        <state state='open'/>
        <service name='ssh' product='OpenSSH' version='8.9p1'/>
      </port>
      <port protocol='tcp' portid='80'>
        <state state='open'/>
        <service name='http' product='nginx' version='1.18.0'/>
      </port>
    </ports>
  </host>
</nmaprun>
"""

MULTI_HOST_XML = """<?xml version='1.0'?>
<nmaprun>
  <runstats>
    <hosts up='3' down='0' total='3'/>
  </runstats>
  <host>
    <address addr='192.168.1.1' addrtype='ipv4'/>
    <status state='up'/>
    <ports>
      <port protocol='tcp' portid='22'><state state='open'/><service name='ssh' product='OpenSSH' version='8.9p1'/></port>
    </ports>
  </host>
  <host>
    <address addr='192.168.1.2' addrtype='ipv4'/>
    <status state='up'/>
    <ports>
      <port protocol='tcp' portid='80'><state state='open'/><service name='http' product='nginx' version='1.18.0'/></port>
    </ports>
  </host>
  <host>
    <address addr='192.168.1.3' addrtype='ipv4'/>
    <status state='up'/>
    <ports>
      <port protocol='tcp' portid='443'><state state='open'/><service name='https' product='Apache httpd' version='2.4.6'/></port>
    </ports>
  </host>
</nmaprun>
"""

ZERO_HOSTS_XML = """<?xml version='1.0'?>
<nmaprun>
  <runstats>
    <hosts up='0' down='0' total='0'/>
  </runstats>
</nmaprun>
"""

def test_parse_nmap_xml():
    metadata = parse_nmap_xml(SAMPLE_XML)
    assert metadata["host_status"] == "up"
    assert len(metadata["open_ports"]) == 2
    ports = {p["port"] for p in metadata["open_ports"]}
    assert ports == {22, 80}
    by_port = {p["port"]: p for p in metadata["open_ports"]}
    assert by_port[22]["product"] == "OpenSSH"
    assert by_port[22]["version"] == "8.9p1"
    assert by_port[80]["product"] == "nginx"
    assert by_port[80]["version"] == "1.18.0"


def test_parse_nmap_xml_with_host_counts():
    """Test that host counts are extracted from runstats."""
    metadata = parse_nmap_xml(SAMPLE_XML)
    assert metadata["hosts_up"] == 1
    assert metadata["hosts_total"] == 1
    assert len(metadata["hosts"]) == 1
    assert metadata["hosts"][0]["ip"] == "192.168.1.1"


def test_parse_nmap_xml_multi_host():
    """Test parsing XML with multiple hosts."""
    metadata = parse_nmap_xml(MULTI_HOST_XML)
    assert metadata["hosts_up"] == 3
    assert metadata["hosts_total"] == 3
    assert len(metadata["hosts"]) == 3
    # Check all hosts are present with their IPs
    ips = {h["ip"] for h in metadata["hosts"]}
    assert ips == {"192.168.1.1", "192.168.1.2", "192.168.1.3"}
    # Check total open ports (one per host)
    assert len(metadata["open_ports"]) == 3
    products = {p.get("product") for p in metadata["open_ports"]}
    versions = {p.get("version") for p in metadata["open_ports"]}
    assert products == {"OpenSSH", "nginx", "Apache httpd"}
    assert versions == {"8.9p1", "1.18.0", "2.4.6"}


def test_parse_nmap_xml_zero_hosts():
    """Test parsing XML when zero hosts were scanned."""
    metadata = parse_nmap_xml(ZERO_HOSTS_XML)
    assert metadata["hosts_up"] == 0
    assert metadata["hosts_total"] == 0
    assert len(metadata["hosts"]) == 0
    assert len(metadata["open_ports"]) == 0


def test_nmap_tool_execution(monkeypatch):
    def fake_run(cmd, capture_output, text, timeout):
        return subprocess.CompletedProcess(cmd, 0, SAMPLE_XML, "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = validate_and_execute_tool(NmapTool(), {"target": "127.0.0.1"})
    assert result.success
    assert result.metadata["host_status"] == "up"
    assert any("nmap" in a for a in result.artifacts)


def test_nmap_multi_target_comma_separated(monkeypatch):
    """Test that comma-separated targets are split into separate arguments."""
    captured_cmd = []
    
    def fake_run(cmd, capture_output, text, timeout):
        captured_cmd.extend(cmd)
        return subprocess.CompletedProcess(cmd, 0, MULTI_HOST_XML, "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = validate_and_execute_tool(
        NmapTool(), 
        {"target": "192.168.1.1,192.168.1.2,192.168.1.3", "ports": "22,80,443"}
    )
    
    assert result.success
    # Verify targets are split into separate arguments (not one combined string)
    assert "192.168.1.1" in captured_cmd
    assert "192.168.1.2" in captured_cmd
    assert "192.168.1.3" in captured_cmd
    # The combined string should NOT be present
    assert "192.168.1.1,192.168.1.2,192.168.1.3" not in captured_cmd


def test_nmap_multi_target_space_separated(monkeypatch):
    """Test that space-separated targets are split into separate arguments."""
    captured_cmd = []
    
    def fake_run(cmd, capture_output, text, timeout):
        captured_cmd.extend(cmd)
        return subprocess.CompletedProcess(cmd, 0, MULTI_HOST_XML, "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = validate_and_execute_tool(
        NmapTool(), 
        {"target": "192.168.1.1 192.168.1.2 192.168.1.3", "ports": "22,80,443"}
    )
    
    assert result.success
    # Verify targets are split into separate arguments
    assert "192.168.1.1" in captured_cmd
    assert "192.168.1.2" in captured_cmd
    assert "192.168.1.3" in captured_cmd


def test_nmap_zero_hosts_returns_failure(monkeypatch):
    """Test that scanning 0 hosts returns failure (not false success)."""
    def fake_run(cmd, capture_output, text, timeout):
        # Nmap exits 0 but scanned 0 hosts (e.g., bad target format)
        return subprocess.CompletedProcess(cmd, 0, ZERO_HOSTS_XML, "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = validate_and_execute_tool(
        NmapTool(), 
        {"target": "bad-target-string", "ports": "1-1000"}
    )
    
    # Should be marked as failure since 0 hosts were scanned
    assert not result.success
    assert "zero_hosts_scanned" in result.metadata.get("warning", "")
    assert "WARNING" in result.stderr


def test_nmap_single_target_unchanged(monkeypatch):
    """Test that single targets are passed unchanged."""
    captured_cmd = []
    
    def fake_run(cmd, capture_output, text, timeout):
        captured_cmd.extend(cmd)
        return subprocess.CompletedProcess(cmd, 0, SAMPLE_XML, "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = validate_and_execute_tool(NmapTool(), {"target": "192.168.1.1"})
    
    assert result.success
    assert "192.168.1.1" in captured_cmd

