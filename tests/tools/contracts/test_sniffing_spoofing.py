import pytest

from .base_contract import BaseToolContract


@pytest.mark.parametrize(
    "tool_id",
    [
        pytest.param(
            "sniffing_spoofing.network_sniffers.tshark",
            marks=pytest.mark.tool("sniffing_spoofing.network_sniffers.tshark"),
        ),
        pytest.param(
            "sniffing_spoofing.network_sniffers.tcpdump",
            marks=pytest.mark.tool("sniffing_spoofing.network_sniffers.tcpdump"),
        ),
        pytest.param(
            "sniffing_spoofing.network_sniffers.netsniff_ng",
            marks=pytest.mark.tool("sniffing_spoofing.network_sniffers.netsniff_ng"),
        ),
        pytest.param(
            "sniffing_spoofing.network_sniffers.dsniff",
            marks=pytest.mark.tool("sniffing_spoofing.network_sniffers.dsniff"),
        ),
        pytest.param(
            "sniffing_spoofing.spoofing_poisoning.arpspoof",
            marks=pytest.mark.tool("sniffing_spoofing.spoofing_poisoning.arpspoof"),
        ),
        pytest.param(
            "sniffing_spoofing.spoofing_poisoning.bettercap",
            marks=pytest.mark.tool("sniffing_spoofing.spoofing_poisoning.bettercap"),
        ),
        pytest.param(
            "sniffing_spoofing.spoofing_poisoning.dnsspoof",
            marks=pytest.mark.tool("sniffing_spoofing.spoofing_poisoning.dnsspoof"),
        ),
        pytest.param(
            "sniffing_spoofing.spoofing_poisoning.ettercap",
            marks=pytest.mark.tool("sniffing_spoofing.spoofing_poisoning.ettercap"),
        ),
        pytest.param(
            "sniffing_spoofing.spoofing_poisoning.responder",
            marks=pytest.mark.tool("sniffing_spoofing.spoofing_poisoning.responder"),
        ),
        pytest.param(
            "sniffing_spoofing.web_sniffers.zaproxy",
            marks=pytest.mark.tool("sniffing_spoofing.web_sniffers.zaproxy"),
        ),
    ],
)
class TestSniffingSpoofingContracts(BaseToolContract):
    @pytest.fixture
    def tool_id(self, request):
        return request.param
