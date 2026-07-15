import pytest

from .base_contract import BaseToolContract


@pytest.mark.parametrize(
    "tool_id",
    [
        pytest.param(
            "maintaining_access.os_backdoors.cymothoa",
            marks=pytest.mark.tool("maintaining_access.os_backdoors.cymothoa"),
        ),
        pytest.param(
            "maintaining_access.web_backdoors.weevely",
            marks=pytest.mark.tool("maintaining_access.web_backdoors.weevely"),
        ),
        pytest.param(
            "maintaining_access.tunneling_pivoting.proxychains",
            marks=pytest.mark.tool("maintaining_access.tunneling_pivoting.proxychains"),
        ),
        pytest.param(
            "maintaining_access.tunneling_pivoting.dns2tcp",
            marks=pytest.mark.tool("maintaining_access.tunneling_pivoting.dns2tcp"),
        ),
        pytest.param(
            "maintaining_access.tunneling_pivoting.iodine",
            marks=pytest.mark.tool("maintaining_access.tunneling_pivoting.iodine"),
        ),
        pytest.param(
            "maintaining_access.tunneling_pivoting.proxytunnel",
            marks=pytest.mark.tool("maintaining_access.tunneling_pivoting.proxytunnel"),
        ),
        pytest.param(
            "maintaining_access.tunneling_pivoting.ptunnel",
            marks=pytest.mark.tool("maintaining_access.tunneling_pivoting.ptunnel"),
        ),
    ],
)
class TestMaintainingAccessContracts(BaseToolContract):
    @pytest.fixture
    def tool_id(self, request):
        return request.param
