"""Deterministic adapter for msfconsole exploitation outcomes.

This adapter normalizes metasploit execution metadata/evidence into:
- finding.exploit_succeeded
- relationship.exploits (only with deterministic source+target subjects)"""

from __future__ import annotations

import ipaddress
import re
from typing import Any, Mapping

from ..contracts import ObservationCreate
from ..identity.canonical_keys import build_relationship_edge_key
from .base import AdapterContext
from .network_common import build_host_subject_key
from .semantic_common import extract_semantic_observations
from .web_common import dedupe_observations, make_observation, resolve_evidence_refs, sanitize_token

_SESSION_OPENED_RE = re.compile(r"session\s+\d+\s+opened", re.IGNORECASE)
_METERPRETER_RE = re.compile(r"meterpreter\s+>", re.IGNORECASE)
_EXPLICIT_SUCCESS_RE = re.compile(r"exploit(?:ation)?\s+completed\s+successfully", re.IGNORECASE)


def _extract_host_from_socket(token: str) -> str:
    """Extract host IP from socket text like 192.168.1.50:4444."""
    value = str(token or "").strip()
    if not value:
        return ""
    host = value.rsplit(":", 1)[0].strip()
    return host


def _normalize_ip(value: Any) -> str:
    """Return normalized IP when input is a valid single IP."""
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        return str(ipaddress.ip_address(raw))
    except ValueError:
        return ""


def _parse_target_from_session_opened_line(line: str) -> str:
    """Parse target host IP from '... -> target:port' metasploit session line."""
    text = str(line or "")
    if "->" not in text:
        return ""
    right = text.split("->", 1)[1].strip()
    target = _extract_host_from_socket(right)
    return _normalize_ip(target)


class MsfconsoleKnowledgeAdapter:
    """Normalize msfconsole payloads into canonical exploitation observations."""

    tool_names = ("exploitation_tools.metasploit.run_exploit",)
    capability_families = ("exploitation", "metasploit")

    def supports(self, context: AdapterContext) -> bool:
        source_tool = context.source_tool_name()
        if source_tool in self.tool_names:
            return True
        return context.capability_family() in self.capability_families

    def extract(self, context: AdapterContext) -> list[ObservationCreate]:
        semantic = extract_semantic_observations(
            context,
            allowed_subject_types_by_observation={
                "finding.exploit_succeeded": {"finding.instance"},
                "relationship.exploits": {"relationship.edge"},
            },
        )
        if semantic:
            return semantic

        execution = context.execution_payload.get("execution")
        execution_dict = dict(execution) if isinstance(execution, Mapping) else {}
        tool_args = execution_dict.get("tool_arguments")
        tool_arguments = dict(tool_args) if isinstance(tool_args, Mapping) else {}
        evidence_refs = resolve_evidence_refs(context)

        exploit_signals = self._collect_exploit_signals(context)
        if not exploit_signals["succeeded"]:
            return []

        module_id = self._resolve_module_identity(context.tool_metadata, tool_arguments)
        target_ip = self._resolve_target_ip(context.tool_metadata, tool_arguments, exploit_signals)
        source_ip = self._resolve_source_ip(tool_arguments, exploit_signals)

        finding_subject_key = self._build_finding_subject_key(
            module_id=module_id,
            target_ip=target_ip,
        )

        finding_payload: dict[str, Any] = {
            "source": "msfconsole",
            "detector_id": module_id,
            "confidence": "confirmed",
            "session_count": exploit_signals["session_count"],
        }
        if target_ip:
            finding_payload["target_ip"] = target_ip
        if source_ip:
            finding_payload["source_ip"] = source_ip
        if evidence_refs:
            finding_payload["evidence_refs"] = evidence_refs

        observations: list[ObservationCreate] = [
            make_observation(
                context=context,
                observation_type="finding.exploit_succeeded",
                subject_type="finding.instance",
                subject_key=finding_subject_key,
                payload=finding_payload,
            )
        ]

        # Relationship only when source and target are both deterministic IP subjects.
        if source_ip and target_ip:
            source_subject_key = build_host_subject_key(source_ip)
            target_subject_key = build_host_subject_key(target_ip)
            relationship_key = build_relationship_edge_key(
                source_subject_key=source_subject_key,
                relationship_type="exploits",
                target_subject_key=target_subject_key,
            )
            relationship_payload: dict[str, Any] = {
                "source_subject_type": "host.ip",
                "source_subject_key": source_subject_key,
                "target_subject_type": "host.ip",
                "target_subject_key": target_subject_key,
                "relationship_type": "exploits",
                "detector_id": module_id,
            }
            if evidence_refs:
                relationship_payload["evidence_refs"] = evidence_refs
            observations.append(
                make_observation(
                    context=context,
                    observation_type="relationship.exploits",
                    subject_type="relationship.edge",
                    subject_key=relationship_key,
                    payload=relationship_payload,
                )
            )

        return dedupe_observations(observations)

    @staticmethod
    def _resolve_module_identity(tool_metadata: Mapping[str, Any], tool_arguments: Mapping[str, Any]) -> str:
        module_path = str(tool_arguments.get("module_path") or "").strip().lower()
        if module_path:
            return sanitize_token(module_path) or "msfconsole"

        modules_loaded = tool_metadata.get("modules_loaded")
        if isinstance(modules_loaded, list) and modules_loaded:
            candidate = str(modules_loaded[0] or "").strip().lower()
            token = sanitize_token(candidate)
            if token:
                return token
        return "msfconsole"

    def _resolve_target_ip(
        self,
        tool_metadata: Mapping[str, Any],
        tool_arguments: Mapping[str, Any],
        exploit_signals: Mapping[str, Any],
    ) -> str:
        for key in ("rhosts", "target"):
            resolved = _normalize_ip(tool_arguments.get(key))
            if resolved:
                return resolved

        # Fallback to session-opened line parsing.
        for line in exploit_signals.get("success_lines", []):
            parsed = _parse_target_from_session_opened_line(str(line))
            if parsed:
                return parsed

        parsed_output = tool_metadata.get("parsed_output")
        if isinstance(parsed_output, Mapping):
            raw_output = str(parsed_output.get("raw_output") or "")
            for raw_line in raw_output.splitlines():
                parsed = _parse_target_from_session_opened_line(raw_line)
                if parsed:
                    return parsed
        return ""

    @staticmethod
    def _resolve_source_ip(tool_arguments: Mapping[str, Any], exploit_signals: Mapping[str, Any]) -> str:
        resolved = _normalize_ip(tool_arguments.get("lhost"))
        if resolved:
            return resolved
        for line in exploit_signals.get("success_lines", []):
            if "opened (" not in str(line):
                continue
            left = str(line).split("opened (", 1)[1].split("->", 1)[0].strip()
            candidate = _extract_host_from_socket(left)
            parsed = _normalize_ip(candidate)
            if parsed:
                return parsed
        return ""

    @staticmethod
    def _build_finding_subject_key(*, module_id: str, target_ip: str) -> str:
        target = target_ip or "unresolved-target"
        return f"finding.instance:msfconsole:{module_id}:target-{sanitize_token(target)}"

    def _collect_exploit_signals(self, context: AdapterContext) -> dict[str, Any]:
        tool_metadata = context.tool_metadata
        session_count = 0
        success_lines: list[str] = []

        parsed_output = tool_metadata.get("parsed_output")
        if isinstance(parsed_output, Mapping):
            sessions = parsed_output.get("sessions")
            if isinstance(sessions, list):
                session_count = max(session_count, len(sessions))
            raw_output = str(parsed_output.get("raw_output") or "")
            for line in raw_output.splitlines():
                if self._is_explicit_success_line(line):
                    success_lines.append(line)

        sessions_created = tool_metadata.get("sessions_created")
        if isinstance(sessions_created, int):
            session_count = max(session_count, sessions_created)

        for artifact in context.artifact_summaries:
            if not isinstance(artifact, Mapping):
                continue
            content_text = artifact.get("content_text")
            if isinstance(content_text, str) and content_text.strip():
                for line in content_text.splitlines():
                    if self._is_explicit_success_line(line):
                        success_lines.append(line)

        succeeded = session_count > 0 or len(success_lines) > 0
        return {
            "succeeded": succeeded,
            "session_count": session_count,
            "success_lines": success_lines,
        }

    @staticmethod
    def _is_explicit_success_line(line: str) -> bool:
        text = str(line or "").strip()
        if not text:
            return False
        if _SESSION_OPENED_RE.search(text):
            return True
        if _METERPRETER_RE.search(text):
            return True
        if _EXPLICIT_SUCCESS_RE.search(text):
            return True
        return False
