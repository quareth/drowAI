import pytest

from .base_contract import BaseToolContract


@pytest.mark.parametrize(
    "tool_id",
    [
        pytest.param(
            "database_assessment.oracle_tools.tnscmd10g",
            marks=pytest.mark.tool("database_assessment.oracle_tools.tnscmd10g"),
        ),
        pytest.param(
            "database_assessment.oracle_tools.oscanner",
            marks=pytest.mark.tool("database_assessment.oracle_tools.oscanner"),
        ),
        pytest.param(
            "database_assessment.oracle_tools.sidguesser",
            marks=pytest.mark.tool("database_assessment.oracle_tools.sidguesser"),
        ),
    ],
)
class TestDatabaseAssessmentContracts(BaseToolContract):
    @pytest.fixture
    def tool_id(self, request):
        return request.param
