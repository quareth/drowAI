import pytest

from .base_contract import BaseToolContract

# Note: Windows-only tools are skipped:
# - mimikatz: Windows-only credential extraction tool

@pytest.mark.parametrize(
    "tool_id",
    [
        pytest.param("password_attacks.online_attacks.ncrack", marks=pytest.mark.tool("password_attacks.online_attacks.ncrack")),
        pytest.param("password_attacks.online_attacks.crowbar", marks=pytest.mark.tool("password_attacks.online_attacks.crowbar")),
        pytest.param("password_attacks.online_attacks.patator", marks=pytest.mark.tool("password_attacks.online_attacks.patator")),
        pytest.param("password_attacks.online_attacks.hydra", marks=pytest.mark.tool("password_attacks.online_attacks.hydra")),
        pytest.param("password_attacks.online_attacks.medusa", marks=pytest.mark.tool("password_attacks.online_attacks.medusa")),
        pytest.param("password_attacks.offline_attacks.john", marks=pytest.mark.tool("password_attacks.offline_attacks.john")),
        pytest.param("password_attacks.offline_attacks.hashcat", marks=pytest.mark.tool("password_attacks.offline_attacks.hashcat")),
        pytest.param("password_attacks.offline_attacks.rainbowcrack", marks=pytest.mark.tool("password_attacks.offline_attacks.rainbowcrack")),
        pytest.param("password_attacks.offline_attacks.samdump2", marks=pytest.mark.tool("password_attacks.offline_attacks.samdump2")),
        pytest.param("password_attacks.offline_attacks.crunch", marks=pytest.mark.tool("password_attacks.offline_attacks.crunch")),
        pytest.param("password_attacks.passing_the_hash.ntlmrelayx", marks=pytest.mark.tool("password_attacks.passing_the_hash.ntlmrelayx")),
        pytest.param("password_attacks.passing_the_hash.passing_the_hash_toolkit", marks=pytest.mark.tool("password_attacks.passing_the_hash.passing_the_hash_toolkit")),
        pytest.param("password_attacks.passing_the_hash.responder", marks=pytest.mark.tool("password_attacks.passing_the_hash.responder")),
    ],
)
class TestPasswordAttackContracts(BaseToolContract):
    @pytest.fixture
    def tool_id(self, request):
        return request.param
