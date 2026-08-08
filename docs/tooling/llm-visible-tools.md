# LLM-Visible Toolset

This document lists the current tool catalog exposed to model planning and self-selection.
These are the tools currently completed for LLM use: their argument contracts,
output parsing, compact result projection, artifact/provenance behavior, and
canonical Knowledge/evidence paths are wired well enough for the agent to
reason over their results. The reusable full-wiring contract, including the
producer-owned semantic envelope and shared Knowledge/compact consumers, is
defined in `docs/architecture/tools.md` under "Current Tool Completion
Reference." The list is generated from
`agent.tools.catalog_visibility.visible_available_tools()` and should be treated
as the prompt-facing subset, not the complete implemented tool registry.
Inclusion indicates functional agent wiring; it does not by itself represent
broad runtime or release certification.

Current count: 17 tools.

- `exploitation_tools.metasploit.inspect_module`
- `exploitation_tools.metasploit.run_exploit`
- `exploitation_tools.metasploit.search_modules`
- `information_gathering.dns.amass`
- `information_gathering.network_discovery.fping`
- `information_gathering.network_discovery.nmap`
- `information_gathering.web_enumeration.http_download`
- `information_gathering.web_enumeration.http_request`
- `service_access.ftp_download`
- `service_access.ftp_list`
- `service_access.ftp_login`
- `service_access.ssh_login`
- `shell.assessment`
- `shell.utility`
- `shell.write_stdin`
- `sniffing_spoofing.network_sniffers.tshark`
- `web_applications.web_crawlers.ffuf`

## DNS Enumeration Scope

`information_gathering.dns.amass` is exposed for graph-free Amass v5 DNS
enumeration. DrowAI consumes the collector's normalized DNS-name/IP mappings,
projects DNS names and IP addresses into existing knowledge assets, and
persists explicit `resolves_to` relationships. It does not import or persist
the Amass Open Asset Model graph.

### Amass Validation Maturity

Amass v5 support is functionally wired for agent selection, task-scoped
execution, DNS/IP parsing, canonical-fact compact projection, and Knowledge
projection.

Validation currently consists of targeted automated coverage and limited
manual runtime testing. Amass has not yet been broadly certified across
long-running enumerations, every execution mode and parameter combination, or
all local and managed-runner environments. Timeout, partial-result, and
high-volume output behavior require additional field validation and polish.

Consumers should preserve Amass enumeration status and result-completeness
metadata when interpreting its results.
