"""Architecture documentation checks for deployment-aware LLM implementation state."""

from __future__ import annotations

from pathlib import Path


_REPO_ROOT = Path(__file__).resolve().parents[3]


def _read(path: str) -> str:
    return (_REPO_ROOT / path).read_text(encoding="utf-8")


def _plain(path: str) -> str:
    return " ".join(_read(path).split())


def test_model_architecture_documents_verified_deployment_aware_state() -> None:
    """Canonical model docs describe implemented state rather than target design."""

    body = _plain("docs/architecture/models.md")
    lowered = body.lower()

    assert "Deployment-aware LLM HLD" in body
    assert "deployment references are authoritative for active text-llm selections" in lowered
    assert "`/api/settings` no longer exposes or accepts OpenAI text-LLM mirrors" in body
    assert "legacy task-switch route is removed" in body
    assert "Tenant-admin connection sharing, relay egress, and dynamic policy routing are deferred" in body
    assert "Embedding provider/model, dimensions, and vector-family fields remain unchanged" in body


def test_runtime_architecture_docs_describe_checkpoint_safe_deployment_refs() -> None:
    """Runtime docs keep graph/checkpoint identity non-secret and deployment-aware."""

    agent = _plain("docs/architecture/agent-architecture.md")
    graph = _plain("docs/architecture/langgraph-graph-architecture.md")
    execution = _plain("docs/architecture/execution-plane.md")

    assert "deployment references plus compatibility provider/model snapshots" in agent
    assert "decrypted credentials or SDK clients" in agent
    assert "V2 deployment runtime selection" in graph
    assert "legacy provider/model checkpoints are not written for new turns" in graph.lower()
    assert "deployment-bound text-LLM selection before graph execution" in execution
    assert "runtime resolver revalidates the deployment reference" in execution


def test_hld_remains_design_source_and_marks_phase_6_non_goals() -> None:
    """HLD remains linked design context while naming deferred scope."""

    body = _plain("docs/devdocs/hldd/deployment-aware-llm-architecture-hld.md")

    assert "Canonical current-state reference | `docs/architecture/models.md`" in body
    assert "No relay or relay secret-delivery abstraction is implemented in Phases 1-6" in body
    assert "tenant-shared connections" in body
    assert "automatic deployment fallback" in body
    assert "Embedding provider/model, dimensions, vector-family" in body
    assert "legacy task-switch route has been removed" in body
