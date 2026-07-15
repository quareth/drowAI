"""Guard reporting services against disallowed generation boundaries."""

from __future__ import annotations

import ast
from importlib.util import resolve_name
from pathlib import Path
import subprocess


REPORTING_BOUNDARY_ROOTS = (
    Path("backend/routers/reporting"),
    Path("backend/services/reporting"),
)
REPORTING_BOUNDARY_FILES = (
    Path("backend/repositories/reporting/__init__.py"),
    Path("backend/repositories/reporting/base.py"),
    Path("backend/repositories/reporting/task_closure_memo_repository.py"),
    Path("backend/repositories/reporting/engagement_report_repository.py"),
    Path("backend/repositories/reporting/engagement_report_job_repository.py"),
    Path("backend/repositories/reporting/report_job_worker_repository.py"),
    Path("backend/repositories/reporting/reporting_retention_repository.py"),
    Path("backend/schemas/reporting.py"),
)

MEMO_GENERATOR_PATH = Path("backend/services/reporting/memo_generator.py")
MEMO_PROMPT_PATH = Path("backend/services/reporting/memo_prompt.py")
REPORT_SECTION_PROMPT_PATH = Path("backend/services/reporting/report_section_prompt.py")
REPORT_SECTION_GENERATOR_PATH = Path(
    "backend/services/reporting/report_section_generator.py"
)
REPORT_GENERATION_SERVICE_PATH = Path(
    "backend/services/reporting/report_generation_service.py"
)
REPORT_WORKER_PATH = Path("backend/services/reporting/report_worker.py")
REPORTS_ROUTER_PATH = Path("backend/routers/reporting/reports.py")

PLANNING_TERMS = ("wa" + "ve",)

REPO_ROOT = Path(__file__).resolve().parents[4]

ALLOWED_MEMO_GENERATOR_LLM_IMPORT_PREFIXES = (
    "agent.providers.llm.core.base",
    "backend.services.llm_provider",
    "core.llm",
    "core.llm.structured_schemas",
)

DIRECT_PROVIDER_OR_GRAPH_IMPORT_PREFIXES = (
    "anthropic",
    "backend.services.embeddings.providers.openai",
    "backend.services.usage_tracking.extractors.anthropic",
    "backend.services.usage_tracking.extractors.openai",
    "langchain",
    "langgraph",
    "openai",
)

LLM_RUNTIME_IMPORT_PREFIXES = (
    "agent.graph.nodes.post_tool_reasoning.core.llm_analysis",
    "agent.graph.utils.llm_resolver",
    "agent.providers.llm",
    "agent.reasoning.llm_",
    "backend.models.llm",
    "backend.routers.llm",
    "backend.schemas.llm",
    "backend.services.llm_provider",
    "core.llm",
)

PROMPT_IMPORT_PREFIXES = (
    "backend.services.knowledge.candidate_extraction.prompting",
    "backend.services.memory.memory_extraction_prompts",
    "core.prompts",
)

PRODUCT_NAMING_ROOTS = (
    Path("backend/routers/reporting"),
    Path("backend/services/reporting"),
    Path("backend/tests/services/reporting"),
    Path("core/prompts/versions/engagement_report_section"),
    Path("core/prompts/versions/task_closure_memo"),
)

PRODUCT_NAMING_FILES = (
    Path("backend/repositories/reporting/__init__.py"),
    Path("backend/repositories/reporting/base.py"),
    Path("backend/repositories/reporting/task_closure_memo_repository.py"),
    Path("backend/repositories/reporting/engagement_report_repository.py"),
    Path("backend/repositories/reporting/engagement_report_job_repository.py"),
    Path("backend/repositories/reporting/report_job_worker_repository.py"),
    Path("backend/repositories/reporting/reporting_retention_repository.py"),
    Path("backend/schemas/reporting.py"),
    Path("backend/tests/repositories/test_reporting_repository_contracts.py"),
    Path("backend/tests/repositories/test_task_closure_memo_repository.py"),
    Path("backend/tests/repositories/test_engagement_report_repository.py"),
    Path("backend/tests/repositories/test_engagement_report_job_repository.py"),
    Path("backend/tests/repositories/test_report_job_worker_repository.py"),
    Path("backend/tests/repositories/test_reporting_retention_repository.py"),
    Path("backend/tests/routers/test_reporting_main_mount.py"),
    Path("backend/tests/routers/test_reporting_memos_router.py"),
    Path("backend/tests/schemas/test_reporting_schemas.py"),
    Path("core/llm/structured_schemas.py"),
    Path("core/llm/tests/test_engagement_report_section_structured_schema.py"),
    Path("core/prompts/tests/test_engagement_report_section_templates.py"),
    Path("core/llm/tests/test_task_closure_memo_structured_schema.py"),
    Path("core/prompts/registry.py"),
    Path("core/prompts/tests/test_task_closure_memo_templates.py"),
)

PLANNING_DOC_ROOTS = (
    Path("docs/plans"),
    Path("docs/roadmap"),
)

FORBIDDEN_IMPORT_PREFIXES_BY_BOUNDARY = {
    "Docker services": (
        "backend.services.docker",
        "backend.services.unified_docker_service",
        "docker",
    ),
    "workspace filesystem helpers": (
        "agent.tools.filesystem",
        "backend.config.workspace_config",
    ),
    "agent execution/runtime modules": (
        "agent.executor",
        "agent.graph",
        "agent.runtime",
        "agent.tool_runtime",
        "agent.tools",
        "kali_executor",
    ),
    "legacy task-owned report modules": ("backend.routers.reports",),
    "worker queue/claim services": (
        "backend.services.cve_indexing.lease_service",
        "backend.services.cve_indexing.scheduler",
        "backend.services.knowledge.ingestion_trigger_service",
        "backend.services.langgraph_chat.checkpoint.interrupt_ticket_service",
        "backend.services.langgraph_chat.checkpoint.turn_workflow_service",
        "backend.services.langgraph_chat.execution.orchestration",
        "backend.services.langgraph_chat.execution.turn_service",
        "backend.services.memory.extraction_trigger",
        "backend.services.runner_control",
        "backend.services.task.graph_retry_service",
    ),
    "report export/render modules": (
        "agent.tools.reporting_tools.report_generation",
        "backend.services.data_plane.export_service",
        "backend.services.reporting.report_generation",
        "backend.services.reporting.report_generator",
        "backend.services.reporting.report_render",
        "backend.services.reporting.report_renderer",
        "core.runbooks.renderer",
    ),
}


def _module_name_for_path(path: Path) -> str:
    return ".".join(path.with_suffix("").parts)


def _package_name_for_path(path: Path) -> str:
    module_name = _module_name_for_path(path)
    if path.name == "__init__.py":
        return module_name
    return module_name.rsplit(".", maxsplit=1)[0]


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    package_name = _package_name_for_path(path)
    imported_modules: set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if node.level:
                imported_modules.add(
                    resolve_name(f"{'.' * node.level}{module}", package_name)
                )
            else:
                imported_modules.add(module)

    return imported_modules


def _imports_by_path(paths: list[Path]) -> dict[Path, set[str]]:
    return {path: _imported_modules(path) for path in paths}


def _reporting_python_files() -> list[Path]:
    root_files = [
        path for root in REPORTING_BOUNDARY_ROOTS for path in root.glob("*.py")
    ]
    explicit_files = [path for path in REPORTING_BOUNDARY_FILES if path.exists()]
    return sorted({*root_files, *explicit_files})


def _report_worker_python_files() -> list[Path]:
    return sorted(Path("backend/services/reporting").glob("report_worker*.py"))


def _report_generation_python_files() -> list[Path]:
    service_files = Path("backend/services/reporting").glob("report_*.py")
    return sorted({REPORTS_ROUTER_PATH, *service_files})


def _matches_any_prefix(imported_module: str, prefixes: tuple[str, ...]) -> bool:
    return imported_module.startswith(prefixes)


def _matches_any_module_boundary(
    imported_module: str, prefixes: tuple[str, ...]
) -> bool:
    return any(
        imported_module == prefix or imported_module.startswith(f"{prefix}.")
        for prefix in prefixes
    )


def _is_allowed_reporting_generator_llm_import(
    path: Path,
    imported_module: str,
) -> bool:
    allowed_generator_paths = {
        MEMO_GENERATOR_PATH,
        REPORT_SECTION_GENERATOR_PATH,
        REPORT_GENERATION_SERVICE_PATH,
    }
    return path in allowed_generator_paths and _matches_any_prefix(
        imported_module,
        ALLOWED_MEMO_GENERATOR_LLM_IMPORT_PREFIXES,
    )


def _is_allowed_memo_prompt_import(path: Path, imported_module: str) -> bool:
    allowed_prompt_paths = {MEMO_PROMPT_PATH, REPORT_SECTION_PROMPT_PATH}
    return path in allowed_prompt_paths and _matches_any_prefix(
        imported_module,
        PROMPT_IMPORT_PREFIXES,
    )


def _is_allowed_report_worker_renderer_import(
    path: Path,
    imported_module: str,
) -> bool:
    return (
        path == REPORT_WORKER_PATH
        and imported_module == "backend.services.reporting.report_renderer"
    )


def _is_allowed_reports_router_generation_service_import(
    path: Path,
    imported_module: str,
) -> bool:
    return (
        path == REPORTS_ROUTER_PATH
        and imported_module == "backend.services.reporting.report_generation_service"
    )


def _implementation_text_files() -> list[Path]:
    return [
        *REPORTING_BOUNDARY_FILES,
        *[path for root in REPORTING_BOUNDARY_ROOTS for path in root.glob("*.py")],
    ]


def _reporting_import_violations(
    imports_by_path: dict[Path, set[str]],
) -> list[str]:
    violations: list[str] = []
    for path, imported_modules in imports_by_path.items():
        for imported_module in sorted(imported_modules):
            if _matches_any_prefix(
                imported_module, DIRECT_PROVIDER_OR_GRAPH_IMPORT_PREFIXES
            ):
                violations.append(
                    f"{path}: imports {imported_module} (direct provider/graph module)"
                )
                continue

            if _matches_any_prefix(imported_module, LLM_RUNTIME_IMPORT_PREFIXES):
                if not _is_allowed_reporting_generator_llm_import(
                    path, imported_module
                ):
                    violations.append(
                        f"{path}: imports {imported_module} (LLM runtime module)"
                    )
                continue

            if _matches_any_prefix(imported_module, PROMPT_IMPORT_PREFIXES):
                if not _is_allowed_memo_prompt_import(path, imported_module):
                    violations.append(
                        f"{path}: imports {imported_module} (prompt module)"
                    )
                continue

            for (
                boundary_name,
                forbidden_prefixes,
            ) in FORBIDDEN_IMPORT_PREFIXES_BY_BOUNDARY.items():
                if _matches_any_module_boundary(imported_module, forbidden_prefixes):
                    if _is_allowed_report_worker_renderer_import(
                        path,
                        imported_module,
                    ):
                        break
                    if _is_allowed_reports_router_generation_service_import(
                        path,
                        imported_module,
                    ):
                        break
                    violations.append(
                        f"{path}: imports {imported_module} ({boundary_name})"
                    )
                    break

    return violations


def _report_generation_usage_violations(
    source_by_path: dict[Path, str],
) -> list[str]:
    violations: list[str] = []
    for path, source in source_by_path.items():
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ImportFrom)
                and node.module == "backend.models.core"
                and any(alias.name == "Report" for alias in node.names)
            ):
                violations.append(
                    f"{path}: imports backend.models.core.Report "
                    "(legacy task-owned reports)"
                )

            if isinstance(node, ast.Attribute) and node.attr == "prepare_task_memo":
                violations.append(
                    f"{path}: calls TaskMemoService.prepare_task_memo "
                    "(report generation must use selected current memos)"
                )

            if isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name) and node.func.id == "Report":
                    violations.append(
                        f"{path}: writes backend.models.core.Report "
                        "(legacy task-owned reports)"
                    )
                elif (
                    isinstance(node.func, ast.Attribute) and node.func.attr == "Report"
                ):
                    violations.append(
                        f"{path}: writes backend.models.core.Report "
                        "(legacy task-owned reports)"
                    )

    return violations


def _report_generation_sources() -> dict[Path, str]:
    return {
        path: path.read_text(encoding="utf-8")
        for path in _report_generation_python_files()
        if path.exists()
    }


def _git_output(*args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def _is_relative_to_any(path: Path, roots: tuple[Path, ...]) -> bool:
    return any(path == root or path.is_relative_to(root) for root in roots)


def _is_product_naming_path(path: Path) -> bool:
    if _is_relative_to_any(path, PLANNING_DOC_ROOTS):
        return False
    return (
        _is_relative_to_any(path, PRODUCT_NAMING_ROOTS) or path in PRODUCT_NAMING_FILES
    )


def _changed_paths() -> dict[Path, str]:
    changed_paths: dict[Path, str] = {}
    for line in _git_output(
        "status", "--porcelain", "--untracked-files=all"
    ).splitlines():
        status = line[:2]
        raw_path = line[3:]
        if " -> " in raw_path:
            raw_path = raw_path.rsplit(" -> ", maxsplit=1)[1]
        path = Path(raw_path)
        if not _is_product_naming_path(path):
            continue
        changed_paths[path] = status
    return changed_paths


def _added_lines(path: Path) -> list[tuple[int | None, str]]:
    diff = _git_output("diff", "HEAD", "--unified=0", "--", path.as_posix())
    added_lines: list[tuple[int | None, str]] = []
    new_line_number: int | None = None

    for line in diff.splitlines():
        if line.startswith("@@"):
            header = line.split(" +", maxsplit=1)[1].split(" ", maxsplit=1)[0]
            start = header.split(",", maxsplit=1)[0]
            new_line_number = int(start)
            continue
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("+"):
            added_lines.append((new_line_number, line[1:]))
            if new_line_number is not None:
                new_line_number += 1
            continue
        if line.startswith("-"):
            continue
        if new_line_number is not None:
            new_line_number += 1

    return added_lines


def _new_file_lines(path: Path) -> list[tuple[int | None, str]]:
    text = (REPO_ROOT / path).read_text(encoding="utf-8")
    return list(enumerate(text.splitlines(), start=1))


def _changed_product_naming_lines() -> list[tuple[Path, int | None, str]]:
    changed_lines: list[tuple[Path, int | None, str]] = []
    for path, status in _changed_paths().items():
        absolute_path = REPO_ROOT / path
        if not absolute_path.exists() or "D" in status:
            continue

        if status == "??" or "A" in status:
            lines = _new_file_lines(path)
        else:
            lines = _added_lines(path)

        if any(term in path.as_posix().lower() for term in PLANNING_TERMS):
            changed_lines.append((path, None, path.as_posix()))
        changed_lines.extend((path, line_number, line) for line_number, line in lines)

    return changed_lines


def _planning_wording_violations(
    changed_lines: list[tuple[Path, int | None, str]],
) -> list[str]:
    return [
        f"{path}{':' + str(line_number) if line_number is not None else ''}: "
        "contains internal planning term; use product terms such as "
        "'engagement report generation', 'task closure memo', 'reporting input', "
        "or 'section plan'"
        for path, line_number, line in changed_lines
        for term in PLANNING_TERMS
        if term in line.lower()
    ]


def test_reporting_imports_stay_inside_allowed_generation_boundaries() -> None:
    reporting_files = _reporting_python_files()
    report_worker_files = _report_worker_python_files()
    assert reporting_files, (
        "Reporting routers/services must exist before boundary checks run."
    )
    assert report_worker_files, "Report worker modules must exist before checks run."
    assert set(report_worker_files) <= set(reporting_files)

    violations = _reporting_import_violations(_imports_by_path(reporting_files))

    assert violations == []


def test_report_generation_code_does_not_call_out_of_scope_services() -> None:
    violations = _report_generation_usage_violations(_report_generation_sources())

    assert violations == []


def test_report_section_generator_uses_only_provider_neutral_llm_boundary() -> None:
    violations = _reporting_import_violations(
        {
            REPORT_SECTION_GENERATOR_PATH: {
                "backend.services.llm_provider",
                "core.llm.structured_schemas",
            }
        }
    )

    assert violations == []


def test_boundary_guardrail_reports_synthetic_docker_runtime_and_agent_imports() -> (
    None
):
    violations = _reporting_import_violations(
        {
            Path("backend/services/reporting/report_worker.py"): {
                "agent.executor",
                "agent.tools.filesystem._helpers",
                "backend.services.unified_docker_service",
            }
        }
    )

    assert violations == [
        "backend/services/reporting/report_worker.py: imports agent.executor "
        "(agent execution/runtime modules)",
        "backend/services/reporting/report_worker.py: imports "
        "agent.tools.filesystem._helpers (workspace filesystem helpers)",
        "backend/services/reporting/report_worker.py: imports "
        "backend.services.unified_docker_service (Docker services)",
    ]


def test_boundary_guardrail_reports_synthetic_task_memo_preparation_call() -> None:
    violations = _report_generation_usage_violations(
        {
            Path("backend/services/reporting/report_generation_service.py"): (
                "async def generate(db):\n"
                "    await TaskMemoService(db).prepare_task_memo(task_id='task-1')\n"
            )
        }
    )

    assert violations == [
        "backend/services/reporting/report_generation_service.py: calls "
        "TaskMemoService.prepare_task_memo "
        "(report generation must use selected current memos)"
    ]


def test_boundary_guardrail_reports_synthetic_legacy_report_write() -> None:
    violations = _report_generation_usage_violations(
        {
            Path("backend/services/reporting/report_worker.py"): (
                "from backend.models.core import Report\n"
                "def persist():\n"
                "    return Report(task_id='task-1')\n"
            )
        }
    )

    assert violations == [
        "backend/services/reporting/report_worker.py: imports "
        "backend.models.core.Report (legacy task-owned reports)",
        "backend/services/reporting/report_worker.py: writes "
        "backend.models.core.Report (legacy task-owned reports)",
    ]


def test_boundary_guardrail_reports_synthetic_direct_provider_sdk_import() -> None:
    violations = _reporting_import_violations(
        {
            REPORT_SECTION_GENERATOR_PATH: {
                "openai",
            }
        }
    )

    assert violations == [
        "backend/services/reporting/report_section_generator.py: imports openai "
        "(direct provider/graph module)"
    ]


def test_reporting_implementation_files_use_product_names() -> None:
    violations = _planning_wording_violations(_changed_product_naming_lines())

    assert violations == [], (
        "Reporting implementation artifacts must use product terms instead of "
        f"internal planning wording: {violations}"
    )


def test_product_wording_guardrail_reports_synthetic_reporting_file_violation() -> None:
    violations = _planning_wording_violations(
        [
            (
                Path("backend/services/reporting/report_generator.py"),
                12,
                f"log.info('starting {'wa' + 've'} report generation')",
            )
        ]
    )

    assert violations == [
        "backend/services/reporting/report_generator.py:12: "
        "contains internal planning term; use product terms such as "
        "'engagement report generation', 'task closure memo', 'reporting input', "
        "or 'section plan'"
    ]
