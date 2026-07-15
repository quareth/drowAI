"""Unit tests for credential-attack deterministic compression helpers."""

from __future__ import annotations

from agent.graph.compression.deterministic.contracts import CompressionInput
from agent.graph.compression.deterministic.credential_attack import (
    HYDRA_TOOL_ID,
    credential_attack_adapter,
    registered_credential_attack_tool_ids,
)
from agent.graph.compression.deterministic.registry import (
    compress_deterministically,
    get_adapter,
)


_HYDRA_STDOUT = """Hydra v9.5 (c) 2023 by van Hauser/THC & David Maciejak

Hydra (https://github.com/vanhauser-thc/thc-hydra) starting at 2024-01-15 10:30:00
[DATA] max 16 tasks per 1 server, overall 16 tasks, 1000 login tries (l:10/p:100), ~63 tries per task
[DATA] attacking ssh://192.168.1.100:22/
[22][ssh] host: 192.168.1.100   login: admin   password: admin123
[22][ssh] host: 192.168.1.100   login: root    password: toor
[STATUS] 750.00 tries/min, 750 tries in 00:01h, 250 to do in 00:01h, 16 active
1 of 1 target successfully completed, 2 valid passwords found
Hydra (https://github.com/vanhauser-thc/thc-hydra) finished at 2024-01-15 10:32:15
"""


def test_credential_attack_adapter_registers_hydra_tool_id() -> None:
    """Visible Hydra resolves to the deterministic credential adapter."""

    assert registered_credential_attack_tool_ids() == (HYDRA_TOOL_ID,)
    assert get_adapter(HYDRA_TOOL_ID) is credential_attack_adapter


def test_hydra_stdout_summary_success_count_and_redacted_proof() -> None:
    """Hydra success output exposes counts and proof without reusable passwords."""

    result = compress_deterministically(
        CompressionInput(
            tool_name=HYDRA_TOOL_ID,
            raw_result={
                "success": True,
                "stdout": _HYDRA_STDOUT,
                "stderr": "",
                "artifacts": [
                    "artifacts/hydra_ssh.txt",
                    {
                        "artifact_id": "artifact-1",
                        "artifact_kind": "object_store",
                        "path": "https://objects.local/private/hydra.txt?X-Amz-Signature=raw",
                    },
                ],
            },
        )
    )

    rendered = _render_result(result)

    assert result.summary == "Hydra ssh against 192.168.1.100:22 confirmed 2 successful logins."
    assert "successful logins: 2; accounts=admin, root" in result.key_findings
    assert result.key_findings[1].startswith(
        "credential: service=ssh host=192.168.1.100 port=22 login=admin "
        "password=<redacted> proof_fingerprint=sha256:"
    )
    assert "attack statistics: login_tries=1000, tries_completed=750" in result.key_findings[3]
    assert "artifact: artifacts/hydra_ssh.txt" in result.key_findings
    assert "artifact: artifact://artifact-1" in result.key_findings
    assert result.decision_evidence[0].startswith(
        "hydra proof: service=ssh host=192.168.1.100 port=22 login=admin "
        "password=<redacted> proof_fingerprint=sha256:"
    )
    assert result.structured_signals[:5] == (
        {"type": "kv_pair", "key": "hydra_outcome", "value": "confirmed"},
        {"type": "kv_pair", "key": "hydra_success_count", "value": 2},
        {"type": "kv_pair", "key": "hydra_service", "value": "ssh"},
        {"type": "kv_pair", "key": "hydra_target_host", "value": "192.168.1.100"},
        {"type": "kv_pair", "key": "hydra_target_port", "value": "22"},
    )
    assert result.completeness == "complete"
    assert result.lossiness_risk == "low"
    assert "admin123" not in rendered
    assert "toor" not in rendered
    assert "X-Amz-Signature" not in rendered


def test_hydra_metadata_uses_semantic_observations_and_evidence_safely() -> None:
    """Adapter includes semantic lines after masking durable secret fields."""

    result = compress_deterministically(
        CompressionInput(
            tool_name=HYDRA_TOOL_ID,
            raw_result={
                "metadata": {
                    "semantic_schema_version": "hydra.v1",
                    "capability_family": "credential_attack",
                    "attack_info": {"service": "ssh", "protocol": "ssh"},
                    "target_info": {"host": "10.0.0.5", "port": 22},
                    "credentials": [
                        {
                            "host": "10.0.0.5",
                            "port": 22,
                            "service": "ssh",
                            "username": "alice",
                            "password": "raw-password-should-not-render",
                            "password_present": True,
                            "source_format": "standard",
                        }
                    ],
                    "statistics": {"successful_login_count": 1},
                    "semantic_evidence": [
                        {
                            "type": "auth_result",
                            "name": "successful_login",
                            "value": "password: raw-password-should-not-render",
                            "source": "hydra",
                        }
                    ],
                    "semantic_observations": [
                        {
                            "observation_type": "finding.vulnerability_confirmed",
                            "subject_key": "finding.vulnerability:service.socket:10.0.0.5/tcp/22:hydra/weak-auth",
                            "payload": {
                                "successful_login_count": 1,
                                "password": "raw-password-should-not-render",
                            },
                        }
                    ],
                }
            },
        )
    )

    rendered = _render_result(result)

    assert result.summary == "Hydra ssh against 10.0.0.5:22 confirmed 1 successful logins."
    assert any(line.startswith("semantic evidence: successful_login=") for line in result.decision_evidence)
    assert any(
        line.startswith("semantic observation: finding.vulnerability_confirmed")
        and "successful_login_count=1" in line
        for line in result.decision_evidence
    )
    assert "raw-password-should-not-render" not in rendered


def test_hydra_empty_success_reports_no_valid_credentials() -> None:
    """Successful Hydra metadata with no credentials remains explicit."""

    result = compress_deterministically(
        CompressionInput(
            tool_name=HYDRA_TOOL_ID,
            raw_result={
                "success": True,
                "metadata": {
                    "semantic_schema_version": "hydra.v1",
                    "capability_family": "credential_attack",
                    "attack_info": {"service": "ssh"},
                    "target_info": {"host": "192.168.1.20", "port": 22},
                    "credentials": [],
                    "statistics": {"successful_login_count": 0, "login_tries_total": 20},
                },
            },
        )
    )

    assert result.summary == "Hydra ssh against 192.168.1.20:22 found no valid credentials."
    assert "successful logins: 0" in result.key_findings
    assert "Hydra reported no valid credentials." in result.key_findings
    assert result.structured_signals[:2] == (
        {"type": "kv_pair", "key": "hydra_outcome", "value": "no_valid_credentials"},
        {"type": "kv_pair", "key": "hydra_success_count", "value": 0},
    )


def test_hydra_lockout_timeout_and_errors_are_bounded_and_redacted() -> None:
    """Lockout, timeout, and error outcomes are represented without raw secrets."""

    lockout = compress_deterministically(
        CompressionInput(
            tool_name=HYDRA_TOOL_ID,
            raw_result={
                "success": False,
                "metadata": {
                    "semantic_schema_version": "hydra.v1",
                    "capability_family": "credential_attack",
                    "attack_info": {"service": "ssh"},
                    "target_info": {"host": "192.168.1.30", "port": 22},
                    "credentials": [],
                    "warnings": ["Too many failures: account locked for user admin"],
                    "errors": ["ERROR: account locked after password: SuperSecret1"],
                },
            },
        )
    )
    timeout = compress_deterministically(
        CompressionInput(
            tool_name=HYDRA_TOOL_ID,
            raw_result={
                "success": False,
                "status": "timeout",
                "exit_code": -2,
                "stderr": "Command timed out after 10 minutes; password: SuperSecret1",
                "metadata": {
                    "semantic_schema_version": "hydra.v1",
                    "capability_family": "credential_attack",
                    "attack_info": {"service": "ssh"},
                    "target_info": {"host": "192.168.1.40", "port": 22},
                    "credentials": [],
                },
            },
        )
    )

    rendered = _render_result(lockout) + " " + _render_result(timeout)

    assert lockout.summary == (
        "Hydra ssh against 192.168.1.30:22 hit a lockout/rate-limit condition "
        "with 0 successful logins."
    )
    assert any(line.startswith("lockout/rate-limit signal:") for line in lockout.key_findings)
    assert lockout.errors == ("ERROR: account locked after password: <redacted>",)
    assert lockout.decision_evidence[0].startswith(
        "hydra proof: outcome=lockout service=ssh host=192.168.1.30 port=22 "
        "proof_fingerprint=sha256:"
    )

    assert timeout.summary == "Hydra ssh against 192.168.1.40:22 timed out with 0 successful logins."
    assert timeout.errors == ("Command timed out after 10 minutes; password: <redacted>",)
    assert timeout.decision_evidence[0].startswith(
        "hydra proof: outcome=timeout service=ssh host=192.168.1.40 port=22 "
        "proof_fingerprint=sha256:"
    )
    assert "SuperSecret1" not in rendered


def _render_result(result: object) -> str:
    return str(result)
