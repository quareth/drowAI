"""Tests for loading markdown runbooks into validated runbook contracts."""

from __future__ import annotations

import pytest

from core.runbooks.errors import (
    RunbookLoadError,
    RunbookParseError,
    RunbookValidationError,
)
from core.runbooks.loader import RunbookLoader
from core.runbooks.models import RunbookStage, RunbookType


def test_valid_tool_runbook_loads_successfully(tmp_path):
    runbook_path = tmp_path / "RUNBOOK.md"
    runbook_path.write_text(
        """---
id: filesystem_artifact_reading
name: Filesystem Artifact Reading
type: tool
version: 1
description: Guides artifact reads for filesystem tools.
trigger_tool_ids:
  - read_file
stages:
  - tool_parameters
---
Read the requested artifact before answering.
""",
        encoding="utf-8",
    )

    loaded = RunbookLoader().load(runbook_path)

    assert loaded.id == "filesystem_artifact_reading"
    assert loaded.type is RunbookType.TOOL
    assert loaded.trigger_tool_ids == ("read_file",)
    assert loaded.stages == (RunbookStage.TOOL_PARAMETERS,)
    assert loaded.body == "Read the requested artifact before answering."


def test_missing_frontmatter_raises_parse_error(tmp_path):
    runbook_path = tmp_path / "RUNBOOK.md"
    runbook_path.write_text("Read the requested artifact.\n", encoding="utf-8")

    with pytest.raises(RunbookParseError):
        RunbookLoader().load(runbook_path)


def test_malformed_frontmatter_raises_parse_error(tmp_path):
    runbook_path = tmp_path / "RUNBOOK.md"
    runbook_path.write_text(
        """---
id: filesystem_artifact_reading
Read the requested artifact.
""",
        encoding="utf-8",
    )

    with pytest.raises(RunbookParseError):
        RunbookLoader().load(runbook_path)


def test_invalid_yaml_raises_parse_error(tmp_path):
    runbook_path = tmp_path / "RUNBOOK.md"
    runbook_path.write_text(
        """---
id: [
---
Read the requested artifact.
""",
        encoding="utf-8",
    )

    with pytest.raises(RunbookParseError):
        RunbookLoader().load(runbook_path)


def test_missing_required_metadata_raises_validation_error(tmp_path):
    runbook_path = tmp_path / "RUNBOOK.md"
    runbook_path.write_text(
        """---
id: filesystem_artifact_reading
name: Filesystem Artifact Reading
type: tool
version: 1
description: Guides artifact reads for filesystem tools.
trigger_tool_ids:
  - read_file
---
Read the requested artifact.
""",
        encoding="utf-8",
    )

    with pytest.raises(RunbookValidationError):
        RunbookLoader().load(runbook_path)


def test_tool_runbook_requires_trigger_tool_ids(tmp_path):
    runbook_path = tmp_path / "RUNBOOK.md"
    runbook_path.write_text(
        """---
id: filesystem_artifact_reading
name: Filesystem Artifact Reading
type: tool
version: 1
description: Guides artifact reads for filesystem tools.
stages:
  - tool_parameters
---
Read the requested artifact.
""",
        encoding="utf-8",
    )

    with pytest.raises(
        RunbookValidationError,
        match="tool runbooks require trigger_tool_ids or trigger_category_ids",
    ):
        RunbookLoader().load(runbook_path)


def test_tool_runbook_can_use_category_trigger_ids(tmp_path):
    runbook_path = tmp_path / "RUNBOOK.md"
    runbook_path.write_text(
        """---
id: web_discovery
name: Web Discovery
type: tool
version: 1
description: Guides web discovery tool selection.
trigger_category_ids:
  - web_applications
stages:
  - tool_selection
---
Choose the right visible web tool.
""",
        encoding="utf-8",
    )

    loaded = RunbookLoader().load(runbook_path)

    assert loaded.trigger_tool_ids == ()
    assert loaded.trigger_category_ids == ("web_applications",)
    assert loaded.stages == (RunbookStage.TOOL_SELECTION,)


def test_procedure_runbook_can_omit_trigger_tool_ids(tmp_path):
    runbook_path = tmp_path / "RUNBOOK.md"
    runbook_path.write_text(
        """---
id: web_recon
name: Web Recon
type: procedure
version: 1
description: Guides web reconnaissance flow.
stages:
  - planner
---
Plan the web reconnaissance flow.
""",
        encoding="utf-8",
    )

    loaded = RunbookLoader().load(runbook_path)

    assert loaded.type is RunbookType.PROCEDURE
    assert loaded.trigger_tool_ids == ()
    assert loaded.stages == (RunbookStage.PLANNER,)


def test_empty_body_is_rejected(tmp_path):
    runbook_path = tmp_path / "RUNBOOK.md"
    runbook_path.write_text(
        """---
id: filesystem_artifact_reading
name: Filesystem Artifact Reading
type: tool
version: 1
description: Guides artifact reads for filesystem tools.
trigger_tool_ids:
  - read_file
stages:
  - tool_parameters
---

""",
        encoding="utf-8",
    )

    with pytest.raises(RunbookValidationError):
        RunbookLoader().load(runbook_path)


def test_missing_runbook_file_raises_load_error(tmp_path):
    with pytest.raises(RunbookLoadError):
        RunbookLoader().load(tmp_path / "RUNBOOK.md")
