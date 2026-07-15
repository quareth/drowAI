"""
Shared fixtures for Metasploit tool tests.
"""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock


@pytest.fixture
def mock_pty_session():
    """Mock PTY session for testing interactive mode."""
    session = MagicMock()
    session.write = AsyncMock()
    session.read = AsyncMock(return_value=b"msf6 > ")
    session.close = AsyncMock()
    return session


@pytest.fixture
def sample_search_output():
    """Sample msfconsole search output."""
    return """
Matching Modules
================

   #  Name                                                 Disclosure Date  Rank       Check  Description
   -  ----                                                 ---------------  ----       -----  -----------
   0  exploit/windows/smb/ms17_010_eternalblue             2017-03-14       average    Yes    MS17-010 EternalBlue SMB Remote Windows Kernel Pool Corruption
   1  exploit/windows/smb/ms17_010_psexec                  2017-03-14       normal     Yes    MS17-010 EternalRomance/EternalSynergy/EternalChampion SMB
   2  auxiliary/admin/smb/ms17_010_command                 2017-03-14       normal     No     MS17-010 EternalRomance/EternalSynergy/EternalChampion SMB
   3  auxiliary/scanner/smb/smb_ms17_010                   2017-03-14       normal     No     MS17-010 SMB RCE Detection

Interact with a module by name or index. For example use 3 or use auxiliary/scanner/smb/smb_ms17_010
"""


@pytest.fixture
def sample_session_output():
    """Sample msfconsole session creation output."""
    return """
[*] Started reverse TCP handler on 192.168.1.100:4444 
[*] Sending stage (175174 bytes) to 192.168.1.50
[+] Session 1 opened (192.168.1.100:4444 -> 192.168.1.50:49158)

meterpreter > 
"""


@pytest.fixture
def sample_exploit_output():
    """Sample exploit execution output."""
    return """
[*] Using configured payload windows/meterpreter/reverse_tcp
[*] 192.168.1.50:445 - Connecting to target for exploitation.
[+] 192.168.1.50:445 - Connection established for exploitation.
[+] 192.168.1.50:445 - Target OS selected valid for OS indicated by SMB reply
[*] 192.168.1.50:445 - CORE raw buffer dump (51 bytes)
[*] 192.168.1.50:445 - 0x00000000  fc e8 82 00 00 00 60 89  e5 31 c0 64 8b 50 30 8b  ......`..1.d.P0.
[+] Exploit completed, 1 session(s) created.

msf6 exploit(windows/smb/ms17_010_eternalblue) > 
"""


@pytest.fixture
def sample_auxiliary_output():
    """Sample auxiliary module output."""
    return """
[+] 192.168.1.50:445 - Host is likely VULNERABLE to MS17-010! - Windows 7 Enterprise 7601 Service Pack 1 x64 (64-bit)
[*] Scanned 1 of 1 hosts (100% complete)
[*] Auxiliary module execution completed

msf6 auxiliary(scanner/smb/smb_ms17_010) > 
"""


@pytest.fixture
def sample_error_output():
    """Sample error output."""
    return """
[-] Unknown command: foobar
[-] Exploit failed: No target specified

msf6 > 
"""


@pytest.fixture
def sample_job_output():
    """Sample background job output."""
    return """
[*] Exploit running as background job 1.
[*] Exploit completed, but no session was created.

[*] Started reverse TCP handler on 192.168.1.100:4444

msf6 exploit(multi/handler) > 
"""


@pytest.fixture
def msf_search_args_class():
    """Get Metasploit module search args class."""
    from agent.tools.exploitation_tools.metasploit.msfconsole import MsfSearchModulesArgs

    return MsfSearchModulesArgs


@pytest.fixture
def msf_inspect_args_class():
    """Get Metasploit module inspection args class."""
    from agent.tools.exploitation_tools.metasploit.msfconsole import MsfInspectModuleArgs

    return MsfInspectModuleArgs


@pytest.fixture
def msf_run_exploit_args_class():
    """Get Metasploit exploit execution args class."""
    from agent.tools.exploitation_tools.metasploit.msfconsole import MsfRunExploitArgs

    return MsfRunExploitArgs
