"""Artifact provenance bridge helpers extracted from tool-execution facade.

This module centralizes provenance start/finalize behavior and provenance
artifact-reference enrichment while preserving existing runtime semantics.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from agent.semantic.enrichment import extract_runtime_semantic_inputs
from agent.tool_runtime.workspace_artifacts import should_persist_workspace_artifact
from runtime_shared.durable_secret_masking import mask_durable_secrets

def get_provenance_service(
    *,
    logger: Any,
) -> Tuple[Optional[Any], Optional[Any]]:
    """Lazy import and create artifact provenance service."""
    try:
        from backend.config.feature_flags import is_artifact_provenance_enabled

        if not is_artifact_provenance_enabled():
            return None, None
        from backend.database import SessionLocal
        from backend.services.artifact.provenance_service import ArtifactProvenanceService

        db = SessionLocal()
        service = ArtifactProvenanceService(db)
        logger.debug("[ARTIFACT_PROVENANCE] Service initialized for current tool execution.")
        return service, db
    except Exception as e:
        logger.warning("[ARTIFACT_PROVENANCE] Failed to initialize service: %s", e)
        return None, None


def build_artifact_ref_label(
    *,
    artifact_kind: str,
    tool_name: str,
    turn_sequence: Optional[int],
    execution_id: Optional[str],
) -> str:
    """Build deterministic artifact label consistent with catalog semantics."""
    normalized_kind = str(artifact_kind or "artifact").strip() or "artifact"
    normalized_tool = str(tool_name or "unknown_tool").strip() or "unknown_tool"
    if isinstance(turn_sequence, int):
        turn_or_execution = f"turn {turn_sequence}"
    elif execution_id:
        turn_or_execution = f"execution {execution_id[:8]}"
    else:
        turn_or_execution = "recent execution"
    return f"{normalized_kind} from {normalized_tool} ({turn_or_execution})"


def normalize_artifact_ref_path(raw_path: Any) -> Optional[str]:
    """Normalize artifact path strings for lightweight lookup matching."""
    if not isinstance(raw_path, str):
        return None
    normalized = raw_path.replace("\\", "/").strip()
    if not normalized:
        return None
    return normalized


def path_lookup_keys(raw_path: Any) -> List[str]:
    """Return normalized path keys including workspace-relative aliases."""
    normalized = normalize_artifact_ref_path(raw_path)
    if not normalized:
        return []
    keys = [normalized]
    workspace_prefix = "/workspace/"
    if normalized.startswith(workspace_prefix):
        relative = normalized[len(workspace_prefix) :]
        if relative and relative not in keys:
            keys.append(relative)
    return keys


def collect_persistable_tool_artifact_paths(
    *,
    raw_artifacts: Any,
    synthetic_output_path: Optional[str],
    path_lookup_keys_fn: Callable[[Any], List[str]],
    normalize_artifact_ref_path_fn: Callable[[Any], Optional[str]],
) -> List[str]:
    """Return unique tool artifact paths suitable for provenance persistence."""
    if not isinstance(raw_artifacts, list):
        return []

    synthetic_keys = set(path_lookup_keys_fn(synthetic_output_path))
    persistable: List[str] = []
    seen: set[str] = set()
    for candidate in raw_artifacts:
        normalized = normalize_artifact_ref_path_fn(candidate)
        if not normalized:
            continue
        if normalized in synthetic_keys:
            continue
        if normalized in seen:
            continue
        seen.add(normalized)
        persistable.append(normalized)
    return persistable


def should_skip_backend_execution_artifact_save(*, outcome: Any) -> bool:
    """Skip backend mirror writes when runner finalizer already materialized artifacts."""
    result = getattr(outcome, "result", None)
    if not isinstance(result, dict):
        return False
    metadata = result.get("metadata")
    if not isinstance(metadata, dict):
        return False
    materialization = metadata.get("artifact_materialization")
    if not isinstance(materialization, dict):
        return False
    materialized_count = materialization.get("materialized_count")
    if isinstance(materialized_count, int) and materialized_count > 0:
        return True
    status = str(materialization.get("status") or "").strip().lower()
    return status in {"succeeded", "partial"}


def is_runner_data_plane_tool_result_metadata(metadata: Any) -> bool:
    """Return True when result metadata came from runner tool-result promotion."""
    if not isinstance(metadata, Mapping):
        return False
    if metadata.get("artifact_scope") == "cloud_data_plane":
        return True
    for key in (
        "artifact_manifest",
        "artifact_upload",
        "artifact_promotion",
        "artifact_promotion_status",
        "task_runtime_job_id",
    ):
        if key in metadata:
            return True
    return False


def resolve_execution_artifact_workspace(
    *,
    workspace_path: Optional[str],
    facts: Any,
) -> Optional[str]:
    """Return the backend workspace where graph-owned artifacts are persisted."""
    if isinstance(workspace_path, str) and workspace_path.strip():
        return workspace_path
    task_id = getattr(facts, "task_id", None)
    if task_id in (None, ""):
        return None
    try:
        from backend.config.workspace_config import WorkspaceConfig

        return str(WorkspaceConfig.ensure_workspace_structure(int(task_id)))
    except Exception:
        return None


def save_execution_artifact(
    *,
    outcome: Any,
    tool_name: str,
    workspace_path: Optional[str],
    facts: Any,
    interactive: Any,
    save_tool_output_artifact_fn: Callable[..., Any],
    safe_inc_fn: Callable[[str], None],
    logger: Any,
) -> Optional[str]:
    """Best-effort artifact save block with unchanged failure tolerance."""
    artifact_path: Optional[str] = None
    try:
        resolved_workspace = resolve_execution_artifact_workspace(
            workspace_path=workspace_path,
            facts=facts,
        )
        if resolved_workspace:
            if should_skip_backend_execution_artifact_save(outcome=outcome):
                logger.debug(
                    "[TOOL_EXECUTION] Skipping backend artifact mirror; runtime workspace already materialized."
                )
                return None

            stdout = outcome.result.get("stdout", "")
            stderr = outcome.result.get("stderr", "")

            current_tool_id = outcome.tool_id or tool_name
            if should_persist_workspace_artifact(current_tool_id):
                artifact_path = save_tool_output_artifact_fn(
                    workspace_path=resolved_workspace,
                    stdout=stdout,
                    stderr=stderr,
                    logger=None,
                )
            else:
                logger.debug(
                    f"[TOOL_EXECUTION] Skipping artifact creation for read-only tool: {current_tool_id}"
                )

            if artifact_path:
                facts.metadata["last_artifact_path"] = artifact_path
                facts.metadata.setdefault("workspace_path", resolved_workspace)
                safe_inc_fn("langgraph_artifact_saves_successful")
            else:
                safe_inc_fn("langgraph_artifact_saves_failed")
                interactive.trace.reasoning.append(
                    "⚠️ Artifact save returned empty path (non-critical)"
                )
    except Exception as exc:
        safe_inc_fn("langgraph_artifact_saves_failed")
        interactive.trace.reasoning.append(
            f"⚠️ Artifact save failed: {exc} (non-critical)"
        )
    return artifact_path


def collect_provenance_artifact_refs(
    *,
    persisted_artifacts: Sequence[Any],
    tool_name: str,
    tool_call_id: Optional[str],
    execution_id: Optional[str],
    turn_sequence: Optional[int],
    build_artifact_ref_label_fn: Callable[..., str],
) -> List[Dict[str, Any]]:
    """Build compact metadata-first refs from persisted artifact provenance rows."""
    collected: List[Dict[str, Any]] = []
    for artifact in persisted_artifacts:
        artifact_id = getattr(artifact, "id", None)
        if artifact_id is None:
            continue
        artifact_kind = str(getattr(artifact, "artifact_kind", "") or "artifact")
        relative_path = getattr(artifact, "relative_path", None)
        source_path = getattr(artifact, "source_path", None)
        fallback_path = getattr(artifact, "fallback_path", None)
        upload_status = str(getattr(artifact, "upload_status", "") or "").strip().lower()
        if upload_status == "upload_pending":
            artifact_promotion_status = "upload_pending"
        elif upload_status in {"upload_failed", "failed"}:
            artifact_promotion_status = "upload_failed"
        else:
            artifact_promotion_status = "ready"
        path_value = relative_path or source_path or fallback_path
        collected.append(
            {
                "artifact_id": str(artifact_id),
                "execution_id": str(execution_id) if execution_id else None,
                "tool_call_id": tool_call_id,
                "tool_name": str(tool_name or "unknown_tool"),
                "artifact_kind": artifact_kind,
                "path": str(path_value) if isinstance(path_value, str) and path_value else None,
                "relative_path": str(relative_path) if isinstance(relative_path, str) and relative_path else None,
                "artifact_promotion_status": artifact_promotion_status,
                "upload_status": upload_status or None,
                "label": build_artifact_ref_label_fn(
                    artifact_kind=artifact_kind,
                    tool_name=tool_name,
                    turn_sequence=turn_sequence,
                    execution_id=execution_id,
                ),
            }
        )
    return collected


def enrich_artifact_refs_with_provenance(
    *,
    refs: Sequence[Mapping[str, Any]],
    provenance_refs: Sequence[Mapping[str, Any]],
    tool_name: str,
    tool_call_id: Optional[str],
    execution_id: Optional[str],
    turn_sequence: Optional[int],
    path_lookup_keys_fn: Callable[[Any], List[str]],
    build_artifact_ref_label_fn: Callable[..., str],
) -> List[Dict[str, Any]]:
    """Enrich compact artifact refs with stable provenance metadata fields."""
    by_artifact_id: Dict[str, Mapping[str, Any]] = {}
    by_path: Dict[str, Mapping[str, Any]] = {}
    for item in provenance_refs:
        artifact_id = item.get("artifact_id")
        if isinstance(artifact_id, str) and artifact_id:
            by_artifact_id[artifact_id] = item
        for key in path_lookup_keys_fn(item.get("path")):
            by_path.setdefault(key, item)
        for key in path_lookup_keys_fn(item.get("relative_path")):
            by_path.setdefault(key, item)

    enriched: List[Dict[str, Any]] = []
    for raw_ref in refs:
        ref = dict(raw_ref)
        matched: Optional[Mapping[str, Any]] = None
        artifact_id = ref.get("artifact_id")
        if isinstance(artifact_id, str) and artifact_id:
            matched = by_artifact_id.get(artifact_id)
        if matched is None:
            for key in path_lookup_keys_fn(ref.get("path")):
                matched = by_path.get(key)
                if matched is not None:
                    break

        if matched is not None:
            for key in (
                "artifact_id",
                "execution_id",
                "tool_call_id",
                "tool_name",
                "artifact_kind",
                "relative_path",
            ):
                matched_value = matched.get(key)
                if not ref.get(key) and matched_value is not None:
                    ref[key] = matched_value
            if not ref.get("path") and matched.get("path") is not None:
                ref["path"] = matched.get("path")

        if not ref.get("execution_id") and execution_id:
            ref["execution_id"] = execution_id
        if not ref.get("tool_call_id") and tool_call_id:
            ref["tool_call_id"] = tool_call_id
        if not ref.get("tool_name"):
            ref["tool_name"] = str(tool_name or "unknown_tool")

        artifact_kind = str(ref.get("artifact_kind") or ("tool_file" if ref.get("path") else "artifact"))
        ref["artifact_kind"] = artifact_kind
        if not ref.get("label"):
            ref["label"] = build_artifact_ref_label_fn(
                artifact_kind=artifact_kind,
                tool_name=str(ref.get("tool_name") or tool_name or "unknown_tool"),
                turn_sequence=turn_sequence,
                execution_id=str(ref.get("execution_id") or execution_id or ""),
            )

        enriched.append(ref)

    return enriched


def record_provenance_execution_start(
    *,
    get_provenance_service_fn: Callable[[], Tuple[Optional[Any], Optional[Any]]],
    request: Any,
    metadata: Mapping[str, Any],
    facts: Any,
    tool_name: str,
    tool_params: Mapping[str, Any],
    tool_call_id: str,
    conversation_id: Optional[str],
    turn_id: Optional[str],
    turn_sequence: Optional[int],
    workspace_path: Optional[str],
    logger: Any,
    safe_inc_fn: Callable[[str], None],
) -> Optional[Any]:
    """Record provenance execution start; always non-fatal."""
    execution_id = None
    provenance_db = None
    try:
        provenance_service, provenance_db = get_provenance_service_fn()
        if provenance_service and request.task_id is not None:
            execution_transport = request.metadata.get("execution_transport", "direct")
            chat_message_id = metadata.get("reserved_message_id")
            if not isinstance(chat_message_id, int):
                chat_message_id = None
            execution_record = provenance_service.record_tool_execution(
                task_id=int(request.task_id),
                tool_name=tool_name,
                tool_arguments=dict(tool_params or {}),
                agent_path="langgraph",
                execution_transport=execution_transport,
                tool_call_id=tool_call_id,
                conversation_id=conversation_id,
                turn_id=turn_id,
                turn_sequence=turn_sequence,
                chat_message_id=chat_message_id,
                workspace_path=workspace_path,
                container_path="/workspace",
                purpose=facts.metadata.get("current_intent"),
                execution_metadata={
                    "capability": facts.capability,
                    "iteration": facts.iterations,
                },
            )
            if execution_record is not None:
                execution_id = execution_record.id
                logger.info(
                    "[ARTIFACT_PROVENANCE] Recorded execution start (execution_id=%s tool_call_id=%s)",
                    execution_id,
                    tool_call_id,
                )
            else:
                logger.warning(
                    "[ARTIFACT_PROVENANCE] Start write returned no execution_id "
                    "(task_id=%s tool=%s tool_call_id=%s).",
                    request.task_id,
                    tool_name,
                    tool_call_id,
                )
        elif provenance_service and request.task_id is None:
            logger.warning(
                "[ARTIFACT_PROVENANCE] Skipping start write: task_id is missing for tool=%s.",
                tool_name,
            )
    except Exception as e:
        logger.error("[ARTIFACT_PROVENANCE] Failed to record execution start: %s", e, exc_info=True)
        safe_inc_fn("artifact_provenance_write_failures")
    finally:
        if provenance_db is not None:
            try:
                provenance_db.close()
            except Exception:
                pass
    return execution_id


def finalize_provenance_execution(
    *,
    get_provenance_service_fn: Callable[[], Tuple[Optional[Any], Optional[Any]]],
    execution_id: Any,
    outcome: Any,
    facts: Any,
    tool_name: str,
    tool_call_id: str,
    turn_sequence: Optional[int],
    workspace_path: Optional[str],
    artifact_path: Optional[str],
    should_persist_artifact_outputs_fn: Callable[[str], bool],
    build_command_for_display_fn: Callable[[str, Mapping[str, Any]], str],
    collect_persistable_tool_artifact_paths_fn: Callable[..., List[str]],
    collect_provenance_artifact_refs_fn: Callable[..., List[Dict[str, Any]]],
    logger: Any,
    safe_inc_fn: Callable[[str], None],
) -> List[Dict[str, Any]]:
    """Complete provenance execution and return persisted artifact refs."""
    persisted_artifact_refs: List[Dict[str, Any]] = []
    provenance_db = None
    try:
        provenance_service, provenance_db = get_provenance_service_fn()
        if provenance_service:
            if outcome.result.get("success"):
                status = "success"
            elif outcome.result.get("validation_errors"):
                status = "validation_error"
            elif outcome.result.get("timeout"):
                status = "timeout"
            else:
                status = "error"
            current_tool_id = str(outcome.tool_id or tool_name)
            persist_output_artifacts = should_persist_artifact_outputs_fn(current_tool_id)
            stdout = outcome.result.get("stdout", "") or ""
            stderr = outcome.result.get("stderr", "") or ""
            runtime_result_metadata = outcome.result.get("metadata")
            runner_data_plane_result = is_runner_data_plane_tool_result_metadata(
                runtime_result_metadata
            )
            command_text = outcome.result.get("command_text")
            if persist_output_artifacts and (
                not isinstance(command_text, str) or not command_text.strip()
            ):
                try:
                    from agent.tools.utils import resolve_command_text_for_execution

                    command_text = resolve_command_text_for_execution(
                        current_tool_id,
                        dict(outcome.parameters or {}),
                        runtime_result_metadata
                        if isinstance(runtime_result_metadata, Mapping)
                        else None,
                    )
                except Exception:
                    command_text = None
                if not isinstance(command_text, str) or not command_text.strip():
                    command_text = build_command_for_display_fn(
                        current_tool_id,
                        dict(outcome.parameters or {}),
                    )
            elif not persist_output_artifacts:
                stdout = ""
                stderr = ""
                command_text = None
            if runner_data_plane_result:
                stdout = ""
                stderr = ""
                command_text = None
            artifact_paths: List[str] = []
            tool_artifacts = outcome.result.get("artifacts", [])
            if persist_output_artifacts and not runner_data_plane_result:
                artifact_paths = collect_persistable_tool_artifact_paths_fn(
                    raw_artifacts=tool_artifacts,
                    synthetic_output_path=artifact_path,
                )
            if not persist_output_artifacts:
                logger.debug(
                    "[ARTIFACT_PROVENANCE] Skipping artifact row persistence for read-only tool: %s",
                    current_tool_id,
                )
            execution_metadata_patch: Dict[str, Any] = {}
            if isinstance(runtime_result_metadata, Mapping):
                tool_metadata = dict(runtime_result_metadata)
                execution_metadata_patch["tool_metadata"] = tool_metadata
                semantic_inputs = extract_runtime_semantic_inputs(tool_metadata)

                if isinstance(tool_metadata.get("semantic_observations"), list):
                    execution_metadata_patch["semantic_observations"] = list(
                        semantic_inputs["semantic_observations"]
                    )

                semantic_evidence = semantic_inputs["semantic_evidence"]
                if isinstance(semantic_evidence, list) and semantic_evidence:
                    execution_metadata_patch["semantic_evidence"] = list(semantic_evidence)

                semantic_schema_version = semantic_inputs["semantic_schema_version"]
                if isinstance(semantic_schema_version, str) and semantic_schema_version.strip():
                    execution_metadata_patch["semantic_schema_version"] = semantic_schema_version

                capability_family = semantic_inputs["capability_family"]
                if isinstance(capability_family, str) and capability_family.strip():
                    execution_metadata_patch["capability_family"] = capability_family

            if persist_output_artifacts and artifact_paths:
                execution_metadata_patch["artifact_refs"] = [
                    {"relative_path": str(path), "artifact_kind": "tool_file"}
                    for path in artifact_paths
                    if isinstance(path, str) and path.strip()
                ]
            if persist_output_artifacts and runner_data_plane_result:
                execution_metadata_patch["artifact_route"] = "runner_data_plane"
                if isinstance(tool_artifacts, list) and tool_artifacts:
                    execution_metadata_patch["legacy_artifact_path_persistence"] = {
                        "status": "skipped",
                        "reason": "runner_data_plane_required",
                        "declared_path_count": len(tool_artifacts),
                    }
            logger.info(
                "[ARTIFACT_PROVENANCE] Attempting completion write "
                "(execution_id=%s status=%s artifact_paths=%s).",
                execution_id,
                status,
                len(artifact_paths),
            )
            # Update execution with actual tool parameters from outcome.
            execution = provenance_service.execution_repo.get_by_id(execution_id)
            if execution is not None:
                masked_tool_arguments = mask_durable_secrets(
                    dict(outcome.parameters or {}),
                    source="provenance_tool_arguments_finalization",
                )
                execution.tool_arguments = (
                    masked_tool_arguments if isinstance(masked_tool_arguments, dict) else {}
                )
                provenance_service.db.flush()
            completed_execution = provenance_service.complete_tool_execution(
                execution_id=execution_id,
                status=status,
                exit_code=outcome.result.get("exit_code"),
                stdout=stdout,
                stderr=stderr,
                command_text=command_text,
                artifact_paths=artifact_paths if artifact_paths else None,
                workspace_path=workspace_path,
                execution_metadata_patch=execution_metadata_patch or None,
            )
            if completed_execution is not None:
                facts.metadata["last_execution_id"] = str(execution_id)
                persisted_rows = provenance_service.artifact_repo.get_by_execution(execution_id)
                persisted_artifact_refs = collect_provenance_artifact_refs_fn(
                    persisted_artifacts=persisted_rows,
                    tool_name=str(outcome.tool_id or tool_name),
                    tool_call_id=tool_call_id,
                    execution_id=str(execution_id),
                    turn_sequence=turn_sequence,
                )
                logger.info(
                    "[ARTIFACT_PROVENANCE] Completed execution (execution_id=%s status=%s artifacts=%s)",
                    execution_id,
                    status,
                    len(artifact_paths),
                )
                safe_inc_fn("artifact_provenance_executions_completed")
            else:
                logger.warning(
                    "[ARTIFACT_PROVENANCE] Completion returned no persisted execution (execution_id=%s).",
                    execution_id,
                )
        else:
            logger.warning(
                "[ARTIFACT_PROVENANCE] Completion skipped: provenance service unavailable "
                "(execution_id=%s).",
                execution_id,
            )
    except Exception as e:
        logger.error("[ARTIFACT_PROVENANCE] Failed to complete execution: %s", e, exc_info=True)
        safe_inc_fn("artifact_provenance_write_failures")
    finally:
        if provenance_db is not None:
            try:
                provenance_db.close()
            except Exception:
                pass
    return persisted_artifact_refs


def finalize_provenance_after_execution_error(
    *,
    get_provenance_service_fn: Callable[[], Tuple[Optional[Any], Optional[Any]]],
    execution_id: Any,
    exc: Exception,
    workspace_path: Optional[str],
    tool_name: str,
    should_persist_artifact_outputs_fn: Callable[[str], bool],
    logger: Any,
    safe_inc_fn: Callable[[str], None],
) -> None:
    """Best-effort finalize when coordinator raises after start write."""
    failure_db = None
    try:
        provenance_service, failure_db = get_provenance_service_fn()
        if provenance_service:
            persist_failure_artifacts = should_persist_artifact_outputs_fn(tool_name)
            completed_execution = provenance_service.complete_tool_execution(
                execution_id=execution_id,
                status="error",
                exit_code=-1,
                stderr=str(exc) if persist_failure_artifacts else None,
                workspace_path=workspace_path,
            )
            if completed_execution is None:
                logger.warning(
                    "[ARTIFACT_PROVENANCE] Coordinator failed and execution remained unfinalized "
                    "(execution_id=%s).",
                    execution_id,
                )
                safe_inc_fn("artifact_provenance_finalize_failures_after_execution_error")
        else:
            logger.warning(
                "[ARTIFACT_PROVENANCE] Coordinator failed but provenance service unavailable "
                "(execution_id=%s).",
                execution_id,
            )
    except Exception as finalize_error:
        logger.error(
            "[ARTIFACT_PROVENANCE] Failed to finalize execution after coordinator error: %s",
            finalize_error,
            exc_info=True,
        )
        safe_inc_fn("artifact_provenance_finalize_failures_after_execution_error")
    finally:
        if failure_db is not None:
            try:
                failure_db.close()
            except Exception:
                pass
