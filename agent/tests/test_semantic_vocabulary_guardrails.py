"""Guardrail tests that lock semantic vocabulary consumption boundaries."""

from __future__ import annotations

import ast
import re
from pathlib import Path

from agent.semantic.evidence_vocabulary import (
    SemanticEvidenceType,
    get_evidence_detail_schema,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
ALLOWED_EVIDENCE_TYPES = {member.value for member in SemanticEvidenceType}
NON_SEMANTIC_TYPE_LITERAL_ALLOWLIST = {
    "agent/tools/builder_intent.py",  # Reserved tool-call JSON schema uses `"type": "string"`.
    "agent/tools/filesystem/stat_path.py",  # Filesystem payload uses POSIX entry types like "file"/"directory".
    "agent/tools/maintaining_access/web_backdoors/php_meterpreter.py",  # Session metadata schema uses handler type labels.
    "agent/tools/stress_testing/network_stress/scapy.py",  # Packet/result records include protocol packet "type" tags.
    "agent/tools/stress_testing/network_stress/siege.py",  # Siege summary rows classify event "type" outside evidence schema.
    "agent/tools/stress_testing/web_stress/httprint.py",  # Fingerprint rows use app signature "type" fields.
    "agent/tools/stress_testing/web_stress/tlssled.py",  # TLS probe records expose finding category "type" values.
    "agent/tools/tool_call_specs.py",  # Tool-call argument schema uses call/input "type" declarations.
    "agent/tools/vulnerability_analysis/cisco_tools/cisco_global_exploiter.py",  # Exploit module metadata uses exploit "type".
    "agent/tools/vulnerability_analysis/cisco_tools/cisco_ocs.py",  # Cisco OCS output parser emits result "type" labels.
    "agent/tools/vulnerability_analysis/cisco_tools/yersinia.py",  # Protocol attack vectors are labeled by protocol "type".
    "agent/tools/vulnerability_analysis/fuzzing/bed.py",  # Fuzz case descriptors use payload "type" fields.
    "agent/tools/vulnerability_analysis/fuzzing/powerfuzzer.py",  # Fuzzer results classify input "type" metadata.
    "agent/tools/vulnerability_analysis/fuzzing/sfuzz.py",  # sfuzz payload corpus records include case "type".
    "agent/tools/vulnerability_analysis/fuzzing/spike.py",  # SPIKE templates encode primitive "type" tokens.
    "agent/tools/vulnerability_analysis/voip_analysis/svmap.py",  # SIP enumeration rows store target "type" labels.
    "agent/tools/web_applications/web_vulnerability_scanners/sqlmap.py",  # SQLMap dump/schema metadata uses table/column "type".
}
TOOL_SPECIFIC_COMPRESSION_MODULES = {
    "agent/graph/compression/deterministic/filesystem.py",
    "agent/graph/compression/deterministic/http.py",
    "agent/graph/compression/deterministic/metasploit.py",
    "agent/graph/compression/deterministic/network_discovery.py",
    "agent/graph/compression/deterministic/tests/test_contracts.py",
    "agent/graph/compression/deterministic/utility.py",
}
POLICY_CONSTANT_NAMES = (
    "EVIDENCE_PER_TYPE_LIMIT",
    "_SEMANTIC_EVIDENCE_GLOBAL_LIMIT",
    "EVIDENCE_DETAIL_SCHEMA",
    "SEMANTIC_EVIDENCE_NAME_MAX_LEN",
    "SEMANTIC_EVIDENCE_VALUE_MAX_LEN",
    "SEMANTIC_EVIDENCE_DETAIL_VALUE_MAX_LEN",
    "SEMANTIC_EVIDENCE_DETAIL_MAX_KEYS",
)


def _line_number_for_offset(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def _iter_files(base: Path, pattern: str) -> list[Path]:
    return sorted(path for path in base.rglob(pattern) if path.is_file())


def test_no_off_vocabulary_type_strings_under_agent_tools() -> None:
    violations: list[tuple[str, int, str]] = []
    for path in _iter_files(REPO_ROOT / "agent" / "tools", "*.py"):
        source = path.read_text(encoding="utf-8-sig")
        tree = ast.parse(source, filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Dict):
                continue
            keyed_values: dict[str, ast.AST] = {}
            for key_node, value_node in zip(node.keys, node.values):
                if isinstance(key_node, ast.Constant) and isinstance(key_node.value, str):
                    keyed_values[key_node.value] = value_node
            if "type" not in keyed_values:
                continue
            type_value_node = keyed_values["type"]
            if not (
                isinstance(type_value_node, ast.Constant)
                and isinstance(type_value_node.value, str)
            ):
                continue
            type_value = type_value_node.value
            if type_value in ALLOWED_EVIDENCE_TYPES:
                continue
            relative_path = path.relative_to(REPO_ROOT).as_posix()
            if relative_path in NON_SEMANTIC_TYPE_LITERAL_ALLOWLIST:
                continue
            violations.append(
                (
                    relative_path,
                    getattr(type_value_node, "lineno", node.lineno),
                    type_value,
                )
            )

    assert not violations, (
        "Found semantic evidence `type` strings outside SemanticEvidenceType. "
        "Use SemanticEvidenceType.<member>.value in tool semantic emitters.\n"
        + "\n".join(
            f"{file_path}:{lineno} uses non-vocabulary type '{type_value}'"
            for file_path, lineno, type_value in violations
        )
    )


def test_no_tool_name_branching_in_consumption_layer() -> None:
    pattern = re.compile(r"""tool_name\s*==|["']ffuf["']\s*in\b|["']nmap["']\s*in\b""")
    targets = [REPO_ROOT / "agent" / "context" / "tool_processor.py"]
    targets.extend(_iter_files(REPO_ROOT / "agent" / "graph" / "compression", "*.py"))
    targets.extend(_iter_files(REPO_ROOT / "core" / "prompts" / "versions" / "tool_output_processing" / "v4", "*"))
    targets.extend(_iter_files(REPO_ROOT / "agent" / "semantic", "*.py"))

    violations: list[str] = []
    for path in targets:
        relative_path = path.relative_to(REPO_ROOT).as_posix()
        if relative_path in TOOL_SPECIFIC_COMPRESSION_MODULES:
            continue
        text = path.read_text(encoding="utf-8")
        for match in pattern.finditer(text):
            line_no = _line_number_for_offset(text, match.start())
            violations.append(
                f"{relative_path}:{line_no} matched `{match.group(0)}`"
            )

    assert not violations, (
        "Consumption layer must stay tool-agnostic. Remove tool-name checks and route tool-specific "
        "logic through tool-local semantic emitters.\n" + "\n".join(violations)
    )


def test_tool_name_branching_pattern_catches_no_space_in_membership() -> None:
    pattern = re.compile(r"""tool_name\s*==|["']ffuf["']\s*in\b|["']nmap["']\s*in\b""")
    assert pattern.search('if "ffuf"in tool_name:')
    assert pattern.search('if "nmap"in allowed_tools:')


def test_no_backend_imports_in_semantic_service() -> None:
    pattern = re.compile(r"^\s*(from|import)\s+backend\b", re.MULTILINE)
    violations: list[str] = []
    for path in _iter_files(REPO_ROOT / "agent" / "semantic", "*.py"):
        text = path.read_text(encoding="utf-8")
        for match in pattern.finditer(text):
            line_no = _line_number_for_offset(text, match.start())
            violations.append(f"{path.relative_to(REPO_ROOT)}:{line_no} imports backend")

    assert not violations, (
        "agent/semantic must remain backend-free. Move backend-dependent behavior to integration layers.\n"
        + "\n".join(violations)
    )


def test_detail_schema_covers_every_enum_member() -> None:
    schema_members = {
        evidence_type
        for evidence_type in SemanticEvidenceType
        if isinstance(get_evidence_detail_schema(evidence_type), frozenset)
    }
    assert schema_members == set(SemanticEvidenceType), (
        "EVIDENCE_DETAIL_SCHEMA must include exactly one entry per SemanticEvidenceType member."
    )


def test_detail_schema_has_no_name_or_value_keys() -> None:
    violations = [
        evidence_type.value
        for evidence_type in SemanticEvidenceType
        for allowed_keys in [get_evidence_detail_schema(evidence_type)]
        if "name" in allowed_keys or "value" in allowed_keys
    ]
    assert not violations, (
        "EVIDENCE_DETAIL_SCHEMA is auxiliary-only. Remove `name`/`value` from detail keys for: "
        + ", ".join(sorted(violations))
    )


def test_policy_constants_used_only_inside_authority_modules() -> None:
    allowed_files = {
        REPO_ROOT / "agent" / "semantic" / "evidence_vocabulary.py",
        REPO_ROOT / "agent" / "semantic" / "enrichment.py",
    }

    violations: list[str] = []
    for path in _iter_files(REPO_ROOT / "agent", "*.py"):
        source = path.read_text(encoding="utf-8-sig")
        tree = ast.parse(source, filename=str(path))
        for node in ast.walk(tree):
            constant_name: str | None = None
            if isinstance(node, ast.Name) and node.id in POLICY_CONSTANT_NAMES:
                constant_name = node.id
            elif isinstance(node, ast.Attribute) and node.attr in POLICY_CONSTANT_NAMES:
                constant_name = node.attr
            elif (
                isinstance(node, ast.ImportFrom)
                and node.module == "agent.semantic.evidence_vocabulary"
            ):
                for alias in node.names:
                    if alias.name in POLICY_CONSTANT_NAMES and path not in allowed_files:
                        violations.append(
                            f"{path.relative_to(REPO_ROOT)}:{node.lineno} references `{alias.name}`"
                        )
                continue

            if constant_name and path not in allowed_files:
                violations.append(
                    f"{path.relative_to(REPO_ROOT)}:{node.lineno} references `{constant_name}`"
                )

    assert not violations, (
        "Semantic evidence policy constants must be owned by evidence_vocabulary.py and consumed only "
        "inside evidence_vocabulary.py and enrichment.py.\n" + "\n".join(violations)
    )


def test_renderers_do_not_invoke_validator() -> None:
    enrichment_path = REPO_ROOT / "agent" / "semantic" / "enrichment.py"
    source = enrichment_path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    target_functions = {
        "render_semantic_observations_for_prompt",
        "render_semantic_evidence_for_prompt",
    }
    violations: list[str] = []
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef) or node.name not in target_functions:
            continue
        for call in ast.walk(node):
            if not isinstance(call, ast.Call):
                continue
            if isinstance(call.func, ast.Name) and call.func.id == "validate_semantic_evidence_entries":
                violations.append(node.name)
            if isinstance(call.func, ast.Attribute) and call.func.attr == "validate_semantic_evidence_entries":
                violations.append(node.name)

    assert not violations, (
        "Prompt renderers must remain format-only and must not call validate_semantic_evidence_entries. "
        f"Remove validator calls from: {sorted(set(violations))}"
    )
