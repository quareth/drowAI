# Complete Registered Toolset

This document lists executable tools currently discovered by `agent.tools.tool_registry.available_tools()`.
These are code-defined `BaseTool` implementations. Some are intentionally hidden from
LLM-facing planner catalogs and remain available only to direct/internal runtime flows when
policy and context allow them.

Current count: 183 tools.

## artifact

- `artifact.read`
- `artifact.search`

## database_assessment

- `database_assessment.oracle_tools.oscanner`
- `database_assessment.oracle_tools.sidguesser`
- `database_assessment.oracle_tools.tnscmd10g`

## exploitation_tools

- `exploitation_tools.exploit_databases.exploit_db`
- `exploitation_tools.exploit_databases.findsploit`
- `exploitation_tools.exploit_databases.searchsploit`
- `exploitation_tools.metasploit.inspect_module`
- `exploitation_tools.metasploit.msfdb`
- `exploitation_tools.metasploit.msfvenom`
- `exploitation_tools.metasploit.run_exploit`
- `exploitation_tools.metasploit.search_modules`
- `exploitation_tools.payload_creation.powersploit`
- `exploitation_tools.payload_creation.shellnoob`
- `exploitation_tools.payload_creation.shellter`
- `exploitation_tools.payload_creation.the_backdoor_factory`
- `exploitation_tools.payload_creation.veil`

## filesystem

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

## forensics

- `forensics.digital_forensics.bulk_extractor`
- `forensics.digital_forensics.foremost`
- `forensics.digital_forensics.sleuthkit`
- `forensics.digital_forensics.volatility`
- `forensics.forensics_analysis_tools.binwalk`
- `forensics.forensics_analysis_tools.chkrootkit`
- `forensics.forensics_analysis_tools.hashdeep`
- `forensics.forensics_carving_tools.ddrescue`
- `forensics.forensics_carving_tools.photorec`
- `forensics.forensics_carving_tools.safecopy`
- `forensics.forensics_carving_tools.scalpel`
- `forensics.forensics_carving_tools.testdisk`

## information_gathering

- `information_gathering.dns.amass`
- `information_gathering.dns.dnsenum`
- `information_gathering.dns.dnsmap`
- `information_gathering.dns.dnsrecon`
- `information_gathering.dns.fierce`
- `information_gathering.dns.sublist3r`
- `information_gathering.dns.theharvester`
- `information_gathering.network_discovery.fping`
- `information_gathering.network_discovery.masscan`
- `information_gathering.network_discovery.nmap`
- `information_gathering.network_discovery.unicornscan`
- `information_gathering.network_discovery.zmap`
- `information_gathering.osint.censys`
- `information_gathering.osint.dmitry`
- `information_gathering.osint.ike_scan`
- `information_gathering.osint.recon_ng`
- `information_gathering.osint.shodan`
- `information_gathering.osint.spiderfoot`
- `information_gathering.osint.theharvester`
- `information_gathering.osint.whois`
- `information_gathering.route_analysis.mtr`
- `information_gathering.route_analysis.pathping`
- `information_gathering.route_analysis.tcptraceroute`
- `information_gathering.route_analysis.traceroute`
- `information_gathering.smb_enumeration.enum4linux`
- `information_gathering.smtp_analysis.smtp_user_enum`
- `information_gathering.smtp_analysis.swaks`
- `information_gathering.web_enumeration.http_download`
- `information_gathering.web_enumeration.http_request`

## knowledge

- `knowledge.cve_lookup`

## maintaining_access

- `maintaining_access.os_backdoors.cymothoa`
- `maintaining_access.os_backdoors.intersect`
- `maintaining_access.os_backdoors.powersploit`
- `maintaining_access.tunneling_pivoting.dns2tcp`
- `maintaining_access.tunneling_pivoting.iodine`
- `maintaining_access.tunneling_pivoting.proxychains`
- `maintaining_access.tunneling_pivoting.proxytunnel`
- `maintaining_access.tunneling_pivoting.ptunnel`
- `maintaining_access.web_backdoors.php_meterpreter`
- `maintaining_access.web_backdoors.weevely`

## networking_utilities

- `networking_utilities.network`

## password_attacks

- `password_attacks.offline_attacks.crunch`
- `password_attacks.offline_attacks.hashcat`
- `password_attacks.offline_attacks.john`
- `password_attacks.offline_attacks.rainbowcrack`
- `password_attacks.offline_attacks.samdump2`
- `password_attacks.online_attacks.crowbar`
- `password_attacks.online_attacks.hydra`
- `password_attacks.online_attacks.medusa`
- `password_attacks.online_attacks.ncrack`
- `password_attacks.online_attacks.patator`
- `password_attacks.passing_the_hash.mimikatz`
- `password_attacks.passing_the_hash.ntlmrelayx`
- `password_attacks.passing_the_hash.passing_the_hash_toolkit`
- `password_attacks.passing_the_hash.responder`

## reporting_tools

- `reporting_tools.report_generation.dumpzilla`
- `reporting_tools.report_generation.metagoofil`
- `reporting_tools.report_generation.serpico`

## reverse_engineering

- `reverse_engineering.debuggers.gdb`
- `reverse_engineering.disassemblers.binwalk`
- `reverse_engineering.disassemblers.objdump`
- `reverse_engineering.disassemblers.radare2`

## service_access

- `service_access.ftp_download`
- `service_access.ftp_list`
- `service_access.ftp_login`
- `service_access.ssh_login`

## shell

- `shell.exec`
- `shell.script`

## sniffing_spoofing

- `sniffing_spoofing.network_sniffers.dsniff`
- `sniffing_spoofing.network_sniffers.netsniff_ng`
- `sniffing_spoofing.network_sniffers.tcpdump`
- `sniffing_spoofing.network_sniffers.tshark`
- `sniffing_spoofing.spoofing_poisoning.arpspoof`
- `sniffing_spoofing.spoofing_poisoning.bettercap`
- `sniffing_spoofing.spoofing_poisoning.dnsspoof`
- `sniffing_spoofing.spoofing_poisoning.ettercap`
- `sniffing_spoofing.spoofing_poisoning.responder`
- `sniffing_spoofing.web_sniffers.zaproxy`

## stress_testing

- `stress_testing.network_stress.hping3`
- `stress_testing.network_stress.scapy`
- `stress_testing.network_stress.siege`
- `stress_testing.network_stress.slowhttptest`
- `stress_testing.web_stress.httprint`
- `stress_testing.web_stress.tlssled`

## system_services

- `system_services.apache_users`
- `system_services.cisco_torch`
- `system_services.copy_router_config`
- `system_services.finger_user_enum`
- `system_services.nbtscan`
- `system_services.rpc_enum`
- `system_services.showmount`
- `system_services.smb_enum`
- `system_services.snmp_enum`

## vulnerability_analysis

- `vulnerability_analysis.cisco_tools.cisco_auditing_tool`
- `vulnerability_analysis.cisco_tools.cisco_global_exploiter`
- `vulnerability_analysis.cisco_tools.cisco_ocs`
- `vulnerability_analysis.cisco_tools.cisco_torch`
- `vulnerability_analysis.cisco_tools.yersinia`
- `vulnerability_analysis.fuzzing.american_fuzzy_lop`
- `vulnerability_analysis.fuzzing.bed`
- `vulnerability_analysis.fuzzing.boofuzz`
- `vulnerability_analysis.fuzzing.peach`
- `vulnerability_analysis.fuzzing.powerfuzzer`
- `vulnerability_analysis.fuzzing.sfuzz`
- `vulnerability_analysis.fuzzing.spike`
- `vulnerability_analysis.openvas.greenbone`
- `vulnerability_analysis.openvas.openvas`
- `vulnerability_analysis.openvas.openvas_cli`
- `vulnerability_analysis.openvas.openvas_manager`
- `vulnerability_analysis.openvas.openvas_scanner`
- `vulnerability_analysis.voip_analysis.enumiax`
- `vulnerability_analysis.voip_analysis.sipvicious`
- `vulnerability_analysis.voip_analysis.svmap`
- `vulnerability_analysis.voip_analysis.voiphopper`

## web_applications

- `web_applications.cms_identification.cmsmap`
- `web_applications.cms_identification.droopescan`
- `web_applications.cms_identification.joomscan`
- `web_applications.cms_identification.whatweb`
- `web_applications.cms_identification.wpscan`
- `web_applications.web_application_fuzzers.clusterd`
- `web_applications.web_application_fuzzers.ffuf`
- `web_applications.web_application_fuzzers.websploit`
- `web_applications.web_application_fuzzers.wfuzz`
- `web_applications.web_application_proxies.mitmproxy`
- `web_applications.web_application_proxies.zaproxy`
- `web_applications.web_crawlers.dirb`
- `web_applications.web_crawlers.ffuf`
- `web_applications.web_crawlers.gobuster`
- `web_applications.web_crawlers.wfuzz`
- `web_applications.web_vulnerability_scanners.arachni`
- `web_applications.web_vulnerability_scanners.commix`
- `web_applications.web_vulnerability_scanners.nikto`
- `web_applications.web_vulnerability_scanners.nuclei`
- `web_applications.web_vulnerability_scanners.skipfish`
- `web_applications.web_vulnerability_scanners.sqlmap`
- `web_applications.web_vulnerability_scanners.w3af`
- `web_applications.web_vulnerability_scanners.wapiti`
- `web_applications.web_vulnerability_scanners.xsser`
