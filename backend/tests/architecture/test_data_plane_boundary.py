"""Data-plane and live-runtime file boundary tests.

Responsibilities:
- Keep live file explorer paths separate from artifact/object-backed data-plane paths.
- Block persistence or import patterns that would reintroduce signed URL or SDK leakage.
- Keep runner package free from backend object-store implementation dependencies.
"""

from __future__ import annotations

import ast
import json
import pathlib
import re


_REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
_RUNTIME_FILE_EXPLORER_PATH = (
    _REPO_ROOT / "backend/services/workspace/runtime_file_explorer_service.py"
)
_ARTIFACT_MEMORY_PATH = _REPO_ROOT / "backend/services/artifact/memory_service.py"
_KNOWLEDGE_ARCHIVE_PATH = _REPO_ROOT / "backend/services/knowledge/archive_service.py"
_KNOWLEDGE_REPLAY_PATH = _REPO_ROOT / "backend/services/knowledge/replay_source_resolver.py"
_EVIDENCE_STORAGE_PATH = _REPO_ROOT / "backend/services/knowledge/evidence_storage_service.py"
_CLOUD_PROVIDER_PATH = _REPO_ROOT / "backend/services/runtime_provider/cloud_runner_provider.py"
_CLOUD_ARTIFACT_OPERATIONS_PATH = (
    _REPO_ROOT / "backend/services/runtime_provider/cloud_runner/operations/artifact.py"
)
_PROVENANCE_MODEL_PATH = _REPO_ROOT / "backend/models/provenance.py"
_KNOWLEDGE_MODEL_PATH = _REPO_ROOT / "backend/models/knowledge.py"
_RUNTIME_MANIFEST_PATH = (
    _REPO_ROOT / "runtime/manifests/runtime-package-manifest.md"
)

_JSON_BLOCK_RE = re.compile(r"```json\s*(\{.*?\})\s*```", flags=re.DOTALL)
_FORBIDDEN_SIGNED_URL_TOKENS = (
    "signed_url",
    "signed_upload_url",
    "signed_download_url",
    "upload_signed_url",
    "download_signed_url",
    "presigned_url",
)
_FORBIDDEN_OBJECT_STORE_SDK_PREFIXES = (
    "boto3",
    "botocore",
    "aioboto3",
    "google.cloud.storage",
    "azure.storage.blob",
    "minio",
    "s3fs",
)
_OBJECT_STORE_IMPLEMENTATION_PATH_PREFIX = "backend/services/data_plane/"
_OBJECT_STORE_IMPLEMENTATION_FILE_NAMES = {
    "object_store.py",
    "local_object_store.py",
}


def _read_text(path: pathlib.Path) -> str:
    return path.read_text(encoding="utf-8")


def _extract_function_body(source: str, func_name: str) -> str:
    start = source.index(f"def {func_name}(")
    next_def = source.find("\ndef ", start + 1)
    if next_def < 0:
        return source[start:]
    return source[start:next_def]


def _extract_async_function_body(source: str, func_name: str) -> str:
    start = source.index(f"async def {func_name}(")
    next_def = source.find("\n    async def ", start + 1)
    if next_def < 0:
        next_def = source.find("\n    def ", start + 1)
    if next_def < 0:
        return source[start:]
    return source[start:next_def]


def _extract_manifest_json(markdown: str) -> dict[str, object]:
    match = _JSON_BLOCK_RE.search(markdown)
    if not match:
        raise AssertionError("Runtime package manifest JSON block is missing.")
    return json.loads(match.group(1))


def _iter_python_files(roots: tuple[str, ...]) -> list[pathlib.Path]:
    files: list[pathlib.Path] = []
    for root in roots:
        root_path = _REPO_ROOT / root
        if root_path.is_file() and root_path.suffix == ".py":
            files.append(root_path)
            continue
        if root_path.is_dir():
            files.extend(sorted(root_path.rglob("*.py")))
    return sorted(set(files))


def _collect_imports(path: pathlib.Path) -> list[tuple[int, str]]:
    tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
    imports: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.append((node.lineno, alias.name))
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                continue
            if node.module:
                imports.append((node.lineno, node.module))
                for alias in node.names:
                    imports.append((node.lineno, f"{node.module}.{alias.name}"))
    return imports


def _matches_prefix(value: str, prefixes: tuple[str, ...]) -> bool:
    return any(value.startswith(prefix) for prefix in prefixes)


def test_runner_file_browser_routes_use_live_runtime_provider_not_data_plane() -> None:
    """File explorer lock: runner-placement browsing uses live runtime provider operations."""
    text = _read_text(_RUNTIME_FILE_EXPLORER_PATH)

    expected_operations = (
        ("_query_runner_workspace_items", "query_runtime_artifacts"),
        ("_read_runner_workspace_file", "read_runtime_artifact_file"),
    )
    assert "ArtifactFileBrowserService" not in text
    assert "backend.services.data_plane" not in text
    for method_name, operation in expected_operations:
        body = _extract_async_function_body(text, method_name)
        assert f'operation="{operation}"' in body
        assert "wait_for_result" in body


def test_artifact_memory_runner_reads_never_use_runtime_workspace_fallback() -> None:
    """Data plane lock: runner-placement artifact reads never call runtime file fallback."""
    text = _read_text(_ARTIFACT_MEMORY_PATH)
    fallback_body = _extract_function_body(text, "_resolve_file_fallback_content")

    assert "if runner_placement or self._is_runner_cloud_execution(execution_payload):" in fallback_body
    assert 'return None, "none", False' in fallback_body
    assert "self._read_runtime_artifact_text(" in fallback_body

    placement_guard_body = _extract_function_body(text, "_task_uses_runner_placement")
    assert "Task.runtime_placement_mode" in placement_guard_body

    transport_guard_body = _extract_function_body(text, "_is_runner_cloud_execution")
    assert 'normalized_transport == "runner_control_channel"' in transport_guard_body


def test_runner_archive_path_avoids_workspace_file_materialization_for_runner_backed_artifacts() -> None:
    """Data plane lock: runner-backed archive branch avoids backend-local durable file writes."""
    text = _read_text(_KNOWLEDGE_ARCHIVE_PATH)

    select_body = _extract_function_body(text, "_select_storage_mode")
    runner_branch_start = select_body.index("if runner_backed and delete_survival_required:")
    runner_branch_end = select_body.index("\n        if is_text:")
    runner_branch = select_body[runner_branch_start:runner_branch_end]

    assert "_materialize_object_reference(" in runner_branch
    assert "self.STORAGE_MODE_OBJECT_REF" in runner_branch
    assert "self.STORAGE_MODE_INLINE_EXCERPT" in runner_branch
    assert "self.STORAGE_MODE_METADATA_ONLY" in runner_branch
    assert "_materialize_archived_file(" not in runner_branch

    existing_row_body = _extract_function_body(
        text, "_ensure_existing_row_meets_delete_survival"
    )
    runner_existing_start = existing_row_body.index("if runner_backed:")
    runner_existing_end = existing_row_body.index("\n        ref = str(row.archived_file_ref")
    runner_existing_branch = existing_row_body[runner_existing_start:runner_existing_end]
    assert "_materialize_archived_file(" not in runner_existing_branch
    assert "row.archived_file_ref = None" in runner_existing_branch


def test_runner_durable_replay_and_storage_paths_do_not_depend_on_workspace_config() -> None:
    """Data plane lock: runner data-plane evidence services avoid WorkspaceConfig path authority."""
    replay_text = _read_text(_KNOWLEDGE_REPLAY_PATH)
    storage_text = _read_text(_EVIDENCE_STORAGE_PATH)

    assert "WorkspaceConfig" not in replay_text
    assert "WorkspaceConfig" not in storage_text
    assert ".write_bytes(" not in storage_text
    assert ".write_text(" not in storage_text


def test_cloud_runner_provider_dispatches_live_workspace_reads() -> None:
    """File explorer lock: cloud runner provider dispatches live workspace read/query messages."""
    provider_text = _read_text(_CLOUD_PROVIDER_PATH)
    artifact_text = _read_text(_CLOUD_ARTIFACT_OPERATIONS_PATH)

    provider_read_body = _extract_async_function_body(
        provider_text, "read_runtime_artifact_file"
    )
    provider_query_body = _extract_async_function_body(
        provider_text, "query_runtime_artifacts"
    )
    read_body = _extract_async_function_body(artifact_text, "read_runtime_artifact_file")
    query_body = _extract_async_function_body(artifact_text, "query_runtime_artifacts")

    assert "self._artifact.read_runtime_artifact_file(request)" in provider_read_body
    assert "self._artifact.query_runtime_artifacts(request)" in provider_query_body
    assert "self._deferred_result(" not in read_body
    assert '"read_runtime_artifact_file"' in read_body
    assert "RunnerMessageType.RUNTIME_WORKSPACE_READ" in read_body
    assert "self._operation_waiter._wait_for_runtime_operation_result(" in read_body

    assert "self._deferred_result(" not in query_body
    assert '"query_runtime_artifacts"' in query_body
    assert "RunnerMessageType.RUNTIME_WORKSPACE_QUERY" in query_body
    assert "self._operation_waiter._wait_for_runtime_operation_result(" in query_body


def test_data_plane_models_do_not_persist_signed_urls() -> None:
    """Data plane lock: ORM data-plane models do not persist signed upload/download URLs."""
    model_texts = (
        _read_text(_PROVENANCE_MODEL_PATH).lower(),
        _read_text(_KNOWLEDGE_MODEL_PATH).lower(),
    )
    offenders: list[str] = []
    for file_path, text in (
        (_PROVENANCE_MODEL_PATH, model_texts[0]),
        (_KNOWLEDGE_MODEL_PATH, model_texts[1]),
    ):
        for token in _FORBIDDEN_SIGNED_URL_TOKENS:
            if token in text:
                offenders.append(f"{file_path.relative_to(_REPO_ROOT)} contains `{token}`")

    assert not offenders, (
        "Data-plane ORM models must persist object keys/metadata only; signed URLs "
        "must be generated just in time. Found:\n  - " + "\n  - ".join(offenders)
    )


def test_object_store_sdk_imports_are_limited_to_object_store_implementations() -> None:
    """Data plane lock: direct object-store SDK imports stay inside object-store implementation modules."""
    roots = ("backend", "agent", "drowai_runner", "runtime_shared")
    offenders: list[str] = []
    for file_path in _iter_python_files(roots):
        rel_path = file_path.relative_to(_REPO_ROOT).as_posix()
        allowed = rel_path.startswith(_OBJECT_STORE_IMPLEMENTATION_PATH_PREFIX) and (
            pathlib.Path(rel_path).name in _OBJECT_STORE_IMPLEMENTATION_FILE_NAMES
        )
        for line_number, imported_module in _collect_imports(file_path):
            if not _matches_prefix(imported_module, _FORBIDDEN_OBJECT_STORE_SDK_PREFIXES):
                continue
            if allowed:
                continue
            offenders.append(
                f"{rel_path}:{line_number}: import `{imported_module}`"
            )

    assert not offenders, (
        "Direct object-store SDK imports must stay inside object-store implementation "
        "modules. Found:\n  - " + "\n  - ".join(offenders)
    )


def test_runner_package_blocks_backend_object_store_server_imports() -> None:
    """Data plane lock: runner package must not import backend object-store server modules."""
    manifest = _extract_manifest_json(_read_text(_RUNTIME_MANIFEST_PATH))
    runner_package = manifest.get("runner_package", {})
    runner_roots = tuple(runner_package.get("python_roots", []))
    offenders: list[str] = []

    for file_path in _iter_python_files(runner_roots):
        rel_path = file_path.relative_to(_REPO_ROOT).as_posix()
        for line_number, imported_module in _collect_imports(file_path):
            if not imported_module.startswith("backend.services.data_plane"):
                continue
            offenders.append(
                f"{rel_path}:{line_number}: import `{imported_module}`"
            )

    assert not offenders, (
        "Runner package modules must not import backend data-plane object-store server "
        "implementations. Found:\n  - " + "\n  - ".join(offenders)
    )
