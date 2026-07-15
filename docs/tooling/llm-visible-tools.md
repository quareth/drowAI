# LLM-Visible Toolset

This document lists the current tool catalog exposed to model planning and self-selection.
These are the tools currently completed for LLM use: their argument contracts,
output parsing, compact result projection, artifact/provenance behavior, and
knowledge/evidence-layer hooks are wired well enough for the agent to reason
over their results. The list is generated from
`agent.tools.catalog_visibility.visible_available_tools()` and should be treated
as the prompt-facing subset, not the complete implemented tool registry.

Current count: 29 tools.

- `exploitation_tools.metasploit.inspect_module`
- `exploitation_tools.metasploit.run_exploit`
- `exploitation_tools.metasploit.search_modules`
- `filesystem.append_file`
- `filesystem.copy_path`
- `filesystem.delete_path`
- `filesystem.edit_lines`
- `filesystem.find_paths`
- `filesystem.grep`
- `filesystem.list_dir`
- `filesystem.make_dir`
- `filesystem.move_path`
- `filesystem.read_file`
- `filesystem.read_head`
- `filesystem.read_tail`
- `filesystem.search_text`
- `filesystem.stat_path`
- `filesystem.write_file`
- `information_gathering.network_discovery.fping`
- `information_gathering.network_discovery.nmap`
- `information_gathering.web_enumeration.http_download`
- `information_gathering.web_enumeration.http_request`
- `networking_utilities.network`
- `service_access.ftp_download`
- `service_access.ftp_list`
- `service_access.ftp_login`
- `service_access.ssh_login`
- `sniffing_spoofing.network_sniffers.tshark`
- `web_applications.web_crawlers.ffuf`
