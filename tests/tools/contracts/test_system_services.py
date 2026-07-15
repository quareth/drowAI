import pytest

from .base_contract import BaseToolContract


@pytest.mark.parametrize(
    "tool_id",
    [
        pytest.param(
            "system_services.showmount",
            marks=pytest.mark.tool("system_services.showmount"),
        ),
        pytest.param(
            "system_services.nbtscan",
            marks=pytest.mark.tool("system_services.nbtscan"),
        ),
        pytest.param(
            "system_services.snmp_enum",
            marks=pytest.mark.tool("system_services.snmp_enum"),
        ),
        pytest.param(
            "system_services.smb_enum",
            marks=pytest.mark.tool("system_services.smb_enum"),
        ),
        pytest.param(
            "system_services.rpc_enum",
            marks=pytest.mark.tool("system_services.rpc_enum"),
        ),
        pytest.param(
            "system_services.finger_user_enum",
            marks=pytest.mark.tool("system_services.finger_user_enum"),
        ),
    ],
)
class TestSystemServicesContracts(BaseToolContract):
    @pytest.fixture
    def tool_id(self, request):
        return request.param
