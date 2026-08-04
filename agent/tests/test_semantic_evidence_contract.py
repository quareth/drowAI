"""Contract tests for shared semantic evidence and the agent adapter."""

from __future__ import annotations

import json
from copy import deepcopy

import pytest

from agent.semantic.enrichment import (
    render_semantic_evidence_for_prompt,
    render_semantic_observations_for_prompt,
    validate_semantic_evidence_entries,
)
from runtime_shared.semantic.pentest_facts import (
    FactDiagnostic,
    SemanticEvidenceType,
    get_evidence_detail_schema,
    get_evidence_per_type_limit,
    get_semantic_evidence_global_limit,
    validate_semantic_evidence,
)


def test_unknown_type_is_dropped() -> None:
    valid, dropped = validate_semantic_evidence_entries(
        [{"type": "not_in_vocab", "name": "foo", "value": "bar"}]
    )

    assert valid == []
    assert dropped == [{"type": "not_in_vocab", "name": "foo", "value": "bar"}]


def test_missing_name_is_dropped() -> None:
    valid, dropped = validate_semantic_evidence_entries(
        [{"type": SemanticEvidenceType.BASELINE.value, "value": "enabled"}]
    )

    assert valid == []
    assert dropped == [{"type": SemanticEvidenceType.BASELINE.value, "value": "enabled"}]


def test_name_and_value_bounded() -> None:
    long_name = "n" * 512
    long_value = "v" * 1024
    valid, dropped = validate_semantic_evidence_entries(
        [
            {
                "type": SemanticEvidenceType.EXECUTION_PARAMETER.value,
                "name": long_name,
                "value": long_value,
            }
        ]
    )

    assert dropped == []
    assert len(valid) == 1
    normalized = valid[0]
    assert normalized["type"] == SemanticEvidenceType.EXECUTION_PARAMETER.value
    assert normalized["name"] == long_name[: len(normalized["name"])]
    assert normalized["value"] == long_value[: len(normalized["value"])]
    assert len(normalized["name"]) < len(long_name)
    assert len(normalized["value"]) < len(long_value)


def test_per_type_cap_applied() -> None:
    execution_parameter_limit = get_evidence_per_type_limit(
        SemanticEvidenceType.EXECUTION_PARAMETER
    )
    entries = [
        {
            "type": SemanticEvidenceType.EXECUTION_PARAMETER.value,
            "name": f"threads_{index}",
            "value": index,
        }
        for index in range(execution_parameter_limit + 2)
    ]
    valid, dropped = validate_semantic_evidence_entries(entries)

    assert len(valid) == execution_parameter_limit
    assert len(dropped) == 2


def test_global_cap_respected(monkeypatch: pytest.MonkeyPatch) -> None:
    patched_limits = {
        evidence_type: 100 for evidence_type in SemanticEvidenceType
    }
    monkeypatch.setattr(
        "runtime_shared.semantic.pentest_facts.evidence.EVIDENCE_PER_TYPE_LIMIT",
        patched_limits,
    )
    entries = [
        {
            "type": SemanticEvidenceType.EXECUTION_PARAMETER.value,
            "name": f"param_{i}",
            "value": i,
        }
        for i in range(40)
    ]

    valid, dropped = validate_semantic_evidence_entries(entries)

    global_limit = get_semantic_evidence_global_limit()
    assert len(valid) == global_limit
    assert len(dropped) == len(entries) - global_limit


def test_validator_is_pure_no_side_effects() -> None:
    entries = [
        {
            "type": SemanticEvidenceType.BASELINE.value,
            "name": "autocalibration",
            "value": "true",
            "detail": {"source": "calibration"},
        }
    ]
    frozen = deepcopy(entries)

    first_valid, first_dropped = validate_semantic_evidence_entries(entries)
    second_valid, second_dropped = validate_semantic_evidence_entries(entries)

    assert entries == frozen
    assert first_valid == second_valid
    assert first_dropped == second_dropped


def test_non_mapping_entry_is_dropped_not_silenced() -> None:
    valid, dropped = validate_semantic_evidence_entries(
        [
            "not-a-mapping",
            {
                "type": SemanticEvidenceType.BASELINE.value,
                "name": "autocalibration",
                "value": True,
            },
        ]
    )

    assert len(valid) == 1
    assert dropped == [{"_invalid_shape": True, "raw": "'not-a-mapping'"}]


def test_detail_schema_unknown_keys_dropped() -> None:
    valid, dropped = validate_semantic_evidence_entries(
        [
            {
                "type": SemanticEvidenceType.BASELINE.value,
                "name": "autocalibration",
                "value": True,
                "detail": {"source": "calibration", "unexpected": "x"},
            }
        ]
    )

    assert dropped == []
    assert valid[0]["detail"] == {"source": "calibration"}


def test_detail_scalar_only_non_scalar_removed() -> None:
    valid, _ = validate_semantic_evidence_entries(
        [
            {
                "type": SemanticEvidenceType.BASELINE.value,
                "name": "baseline",
                "value": "active",
                "detail": {"source": "tool", "note": {"nested": "bad"}},
            }
        ]
    )

    assert valid[0]["detail"] == {"source": "tool"}


@pytest.mark.parametrize("non_finite", (float("nan"), float("inf"), float("-inf")))
@pytest.mark.parametrize("location", ("value", "detail"))
def test_non_finite_evidence_scalars_are_dropped_with_diagnostics(
    non_finite: float,
    location: str,
) -> None:
    invalid_entry = {
        "type": SemanticEvidenceType.RESULT_SUMMARY.value,
        "name": f"invalid_{location}",
        "value": non_finite if location == "value" else 1,
        "detail": (
            {"after_filter_count": non_finite}
            if location == "detail"
            else {}
        ),
    }
    valid_entry = {
        "type": SemanticEvidenceType.BASELINE.value,
        "name": "valid_baseline",
        "value": 1,
    }

    accepted, diagnostics = validate_semantic_evidence(
        [invalid_entry, valid_entry]
    )
    mutable_accepted, dropped = validate_semantic_evidence_entries(
        [invalid_entry, valid_entry]
    )

    assert [entry["name"] for entry in accepted] == ["valid_baseline"]
    assert diagnostics == (
        FactDiagnostic(0, f"invalid_evidence_{location}"),
    )
    assert [entry["name"] for entry in mutable_accepted] == ["valid_baseline"]
    assert len(dropped) == 1
    assert dropped[0]["name"] == f"invalid_{location}"
    json.dumps(mutable_accepted, allow_nan=False)


def test_detail_may_be_empty_after_sanitization() -> None:
    valid, dropped = validate_semantic_evidence_entries(
        [
            {
                "type": SemanticEvidenceType.BASELINE.value,
                "name": "baseline",
                "value": "active",
                "detail": {"not_allowed": "x"},
            }
        ]
    )

    assert dropped == []
    assert valid[0]["detail"] == {}


def test_detail_is_always_present_after_validation() -> None:
    valid, dropped = validate_semantic_evidence_entries(
        [
            {
                "type": SemanticEvidenceType.BASELINE.value,
                "name": "baseline",
                "value": "active",
            },
            {
                "type": SemanticEvidenceType.BASELINE.value,
                "name": "baseline_sanitized",
                "value": "active",
                "detail": {"invalid": "drop"},
            },
        ]
    )

    assert dropped == []
    assert valid[0]["detail"] == {}
    assert valid[1]["detail"] == {}


def test_detail_value_truncated_to_max_len() -> None:
    long_note = "n" * 1024
    valid, dropped = validate_semantic_evidence_entries(
        [
            {
                "type": SemanticEvidenceType.BASELINE.value,
                "name": "baseline",
                "value": "active",
                "detail": {"note": long_note},
            }
        ]
    )

    assert dropped == []
    note = valid[0]["detail"]["note"]
    assert isinstance(note, str)
    assert note == long_note[: len(note)]
    assert len(note) < len(long_note)


def test_detail_schema_covers_every_enum_member() -> None:
    schema_members = {
        evidence_type
        for evidence_type in SemanticEvidenceType
        if isinstance(get_evidence_detail_schema(evidence_type), frozenset)
    }
    assert schema_members == set(SemanticEvidenceType)


def test_detail_schema_has_no_name_or_value_keys() -> None:
    for evidence_type in SemanticEvidenceType:
        allowed_keys = get_evidence_detail_schema(evidence_type)
        assert "name" not in allowed_keys
        assert "value" not in allowed_keys


def test_detail_key_cap_is_consistent_with_schema() -> None:
    for evidence_type in SemanticEvidenceType:
        allowed_keys = get_evidence_detail_schema(evidence_type)
        entry = {
            "type": evidence_type.value,
            "name": f"{evidence_type.value}_detail",
            "value": "ok",
            "detail": {
                **{key: "allowed" for key in allowed_keys},
                **{f"unexpected_{index}": "drop" for index in range(20)},
            },
        }
        valid, dropped = validate_semantic_evidence_entries([entry])
        assert dropped == []
        assert set(valid[0].get("detail", {}).keys()) == set(allowed_keys)


def test_per_type_caps_fit_runtime_global_limit() -> None:
    assert sum(get_evidence_per_type_limit(evidence_type) for evidence_type in SemanticEvidenceType) <= (
        get_semantic_evidence_global_limit()
    )


def test_policy_order_per_type_before_global_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patched_limits = {
        evidence_type: 100 for evidence_type in SemanticEvidenceType
    }
    patched_limits[SemanticEvidenceType.DIAGNOSTIC] = 0
    monkeypatch.setattr(
        "runtime_shared.semantic.pentest_facts.evidence.EVIDENCE_PER_TYPE_LIMIT",
        patched_limits,
    )
    monkeypatch.setattr(
        "runtime_shared.semantic.pentest_facts.evidence.SEMANTIC_EVIDENCE_GLOBAL_LIMIT",
        0,
    )

    _, diagnostics = validate_semantic_evidence(
        [{"type": "diagnostic", "name": "precedence", "value": "locked"}]
    )

    assert diagnostics == (
        FactDiagnostic(0, "evidence_per_type_limit_exceeded"),
    )


def test_render_observations_empty_input_returns_empty_string() -> None:
    assert render_semantic_observations_for_prompt(None) == ""
    assert render_semantic_observations_for_prompt([]) == ""


def test_render_observations_is_stable_and_format_only() -> None:
    observations = [
        {"observation_type": "network.open_port", "port": 443, "detail": {"service": "https", "state": "open"}},
        {"observation_type": "network.open_port", "port": 80, "detail": {"state": "open", "service": "http"}},
    ]

    rendered_first = render_semantic_observations_for_prompt(observations)
    rendered_second = render_semantic_observations_for_prompt(observations)

    assert rendered_first == rendered_second
    assert rendered_first == (
        '[{"detail":{"service":"https","state":"open"},"observation_type":"network.open_port","port":443},'
        '{"detail":{"service":"http","state":"open"},"observation_type":"network.open_port","port":80}]'
    )


def test_render_evidence_empty_or_surprising_inputs_return_empty_string() -> None:
    assert render_semantic_evidence_for_prompt(None) == ""
    assert render_semantic_evidence_for_prompt([]) == ""
    assert render_semantic_evidence_for_prompt([{"name": "x"}]) == ""
    assert render_semantic_evidence_for_prompt([{"type": "unknown", "name": "x"}]) == ""
    assert render_semantic_evidence_for_prompt([{"type": SemanticEvidenceType.BASELINE.value, "name": "x"}, "bad"]) == ""


def test_render_evidence_grouped_by_enum_order_and_not_revalidated() -> None:
    execution_parameter_limit = get_evidence_per_type_limit(
        SemanticEvidenceType.EXECUTION_PARAMETER
    )
    over_limit_entries = [
        {"type": SemanticEvidenceType.EXECUTION_PARAMETER.value, "name": f"threads_{i}", "value": i}
        for i in range(execution_parameter_limit + 1)
    ]
    evidence = [
        {"type": SemanticEvidenceType.BASELINE.value, "name": "autocalibration", "value": True},
        {"type": SemanticEvidenceType.TARGET_TEMPLATE.value, "name": "host", "value": "example.com"},
        *over_limit_entries,
        {"type": SemanticEvidenceType.DIAGNOSTIC.value, "name": "raw_notice", "value": "x" * 400},
    ]

    rendered = render_semantic_evidence_for_prompt(evidence)
    expected = (
        '{"target_template":[{"name":"host","type":"target_template","value":"example.com"}],'
        '"execution_parameter":['
        + ",".join(
            f'{{"name":"threads_{i}","type":"execution_parameter","value":{i}}}'
            for i in range(execution_parameter_limit + 1)
        )
        + '],"baseline":[{"name":"autocalibration","type":"baseline","value":true}],'
        '"diagnostic":[{"name":"raw_notice","type":"diagnostic","value":"'
        + ("x" * 400)
        + '"}]}'
    )

    assert rendered == expected
