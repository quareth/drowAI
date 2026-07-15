"""Offline, deterministic-E2E-only seed command using production service boundaries.

This script is intentionally not an HTTP surface. It mutates only the database
selected by the caller's environment and rejects execution unless deterministic
E2E mode is explicit.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
from typing import Any


_ENABLED_VALUES = frozenset({"1", "true", "yes", "on"})
_MEMBERSHIP_ROLES = ("owner", "admin", "operator", "viewer")
_REPO_ROOT = Path(__file__).resolve().parents[1]


def _require_deterministic_e2e() -> None:
    enabled = str(os.getenv("E2E_DETERMINISTIC_MODE", "")).strip().lower()
    if enabled not in _ENABLED_VALUES:
        raise RuntimeError(
            "Offline seed requires E2E_DETERMINISTIC_MODE=true; refusing non-E2E execution."
        )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Seed isolated deterministic E2E state.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    membership = subparsers.add_parser(
        "membership",
        help="Change an existing user's tenant membership through TenantMembershipService.",
    )
    membership.add_argument("--actor-user-id", required=True, type=int)
    membership.add_argument("--target-user-id", required=True, type=int)
    membership.add_argument("--tenant-id", required=True, type=int)
    membership.add_argument("--role", required=True, choices=_MEMBERSHIP_ROLES)
    tenant_membership = subparsers.add_parser(
        "tenant-membership",
        help="Create one isolated tenant and membership for an existing E2E user.",
    )
    tenant_membership.add_argument("--user-id", required=True, type=int)
    tenant_membership.add_argument("--tenant-slug", required=True)
    tenant_membership.add_argument("--tenant-name", required=True)
    tenant_membership.add_argument("--role", default="owner", choices=_MEMBERSHIP_ROLES)
    workspace = subparsers.add_parser(
        "workspace-knowledge",
        help="Seed one task-local text artifact and projected finding through internal services.",
    )
    workspace.add_argument("--user-id", required=True, type=int)
    workspace.add_argument("--tenant-id", required=True, type=int)
    workspace.add_argument("--engagement-id", required=True, type=int)
    workspace.add_argument("--task-id", required=True, type=int)
    workspace.add_argument("--relative-path", required=True)
    workspace.add_argument("--content", required=True)
    workspace.add_argument("--finding-title", required=True)
    reporting = subparsers.add_parser(
        "reporting-input",
        help="Stop one scoped task and seed a current ready memo from persisted sources.",
    )
    reporting.add_argument("--user-id", required=True, type=int)
    reporting.add_argument("--tenant-id", required=True, type=int)
    reporting.add_argument("--engagement-id", required=True, type=int)
    reporting.add_argument("--task-id", required=True, type=int)
    usage_settings = subparsers.add_parser(
        "usage-settings",
        help="Seed task-scoped usage rows and one suite-owned masked provider credential.",
    )
    usage_settings.add_argument("--user-id", required=True, type=int)
    usage_settings.add_argument("--tenant-id", required=True, type=int)
    usage_settings.add_argument("--task-id", required=True, type=int)
    usage_settings.add_argument("--conversation-id", required=True)
    return parser


def _seed_tenant_membership(
    db: Any,
    *,
    user_id: int,
    tenant_slug: str,
    tenant_name: str,
    role: str,
) -> dict[str, object]:
    """Create an idempotent suite-owned tenant membership without an HTTP surface."""
    from backend.models import Tenant, TenantMembership, User
    from backend.services.tenant.context import DEFAULT_TENANT_ID, TenantContextService
    from sqlalchemy import select

    user = db.execute(select(User).where(User.id == int(user_id))).scalar_one_or_none()
    if user is None:
        raise RuntimeError("Tenant membership user does not exist.")

    slug = str(tenant_slug or "").strip().lower()
    name = str(tenant_name or "").strip()
    normalized_role = str(role or "").strip().lower()
    if not slug or not name:
        raise ValueError("Tenant slug and name are required.")
    if normalized_role not in _MEMBERSHIP_ROLES:
        raise ValueError("Unsupported tenant membership role.")

    TenantContextService(db).ensure_default_tenant()
    tenant = db.execute(select(Tenant).where(Tenant.slug == slug)).scalar_one_or_none()
    if tenant is None:
        tenant = Tenant(slug=slug, name=name, status="active")
        db.add(tenant)
        db.flush()
    elif str(tenant.name) != name:
        raise RuntimeError("Suite-owned tenant slug already exists with a different name.")

    membership = db.execute(
        select(TenantMembership).where(
            TenantMembership.tenant_id == int(tenant.id),
            TenantMembership.user_id == int(user_id),
        )
    ).scalar_one_or_none()
    if membership is None:
        membership = TenantMembership(
            tenant_id=int(tenant.id),
            user_id=int(user_id),
            role=normalized_role,
            status="active",
        )
        db.add(membership)
        db.flush()
    elif str(membership.role) != normalized_role:
        raise RuntimeError("Suite-owned membership already exists with a different role.")

    return {
        "membership_id": int(membership.id),
        "tenant_id": int(tenant.id),
        "tenant_slug": str(tenant.slug),
        "tenant_name": str(tenant.name),
        "user_id": int(membership.user_id),
        "role": str(membership.role),
        "status": str(membership.status),
        "is_default_tenant": int(tenant.id) == int(DEFAULT_TENANT_ID),
    }


def _seed_membership_role(
    db: Any,
    *,
    actor_user_id: int,
    target_user_id: int,
    tenant_id: int,
    role: str,
) -> dict[str, object]:
    from backend.models import TenantMembership
    from backend.services.tenant.context import TenantContextService
    from backend.services.tenant.membership_service import TenantMembershipService
    from sqlalchemy import select

    context_service = TenantContextService(db)
    actor_context = context_service.resolve_for_user(
        user_id=int(actor_user_id),
        requested_tenant_id=int(tenant_id),
        requested_source="offline_e2e_seed",
        allow_ambiguous=False,
    )
    membership = db.execute(
        select(TenantMembership).where(
            TenantMembership.tenant_id == int(tenant_id),
            TenantMembership.user_id == int(target_user_id),
        )
    ).scalar_one_or_none()
    if membership is None:
        raise RuntimeError("Target tenant membership does not exist.")

    updated = TenantMembershipService(db).change_membership_role(
        actor_context=actor_context,
        tenant_id=int(tenant_id),
        membership_id=int(membership.id),
        new_role=str(role),
    )
    return {
        "membership_id": int(updated.membership_id),
        "tenant_id": int(updated.tenant_id),
        "user_id": int(updated.user_id),
        "role": str(updated.role),
        "status": str(updated.status),
    }


def _seed_workspace_knowledge(
    db: Any,
    *,
    user_id: int,
    tenant_id: int,
    engagement_id: int,
    task_id: int,
    relative_path: str,
    content: str,
    finding_title: str,
) -> dict[str, object]:
    """Write task-local content and seed a linked persisted knowledge graph."""
    from datetime import datetime, timezone
    import uuid as uuid_lib

    from backend.models import (
        KnowledgeAsset,
        KnowledgeEntityProvenance,
        KnowledgeEvidenceArchive,
        KnowledgeFinding,
        KnowledgeService,
        Task,
    )
    from backend.services.knowledge.contracts import ObservationCreate
    from backend.services.knowledge.projection_service import KnowledgeProjectionService
    from backend.services.workspace.file_browser_service import FileBrowserService
    from backend.services.workspace.manager import WorkspaceManager
    from sqlalchemy import select

    task = db.execute(
        select(Task).where(
            Task.id == int(task_id),
            Task.user_id == int(user_id),
            Task.tenant_id == int(tenant_id),
            Task.engagement_id == int(engagement_id),
        )
    ).scalar_one_or_none()
    if task is None:
        raise RuntimeError("Task does not match the requested tenant, user, and engagement scope.")

    workspace_manager = WorkspaceManager()
    workspace_path = Path(workspace_manager.create_workspace(int(task_id)))
    target_path = FileBrowserService().validate_path(workspace_path, relative_path)
    if target_path == workspace_path or not target_path.name:
        raise ValueError("A task-local artifact filename is required.")
    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.write_text(str(content), encoding="utf-8")

    now = datetime.now(timezone.utc)
    host_ip = f"192.0.2.{(int(task_id) % 200) + 1}"
    source_execution_id = uuid_lib.uuid4()
    ingestion_run_id = uuid_lib.uuid4()
    evidence_id = uuid_lib.uuid4()
    asset_key = f"host.ip:{host_ip}"
    service_key = f"service.socket:{host_ip}/tcp/8443"
    finding_key = f"finding.vulnerability:{service_key}:e2e-workspace-{int(task_id)}"
    evidence = KnowledgeEvidenceArchive(
        id=evidence_id,
        tenant_id=int(tenant_id),
        user_id=int(user_id),
        engagement_id=int(engagement_id),
        task_id=int(task_id),
        source_execution_id=source_execution_id,
        storage_mode="inline_excerpt",
        inline_excerpt=str(content),
        byte_size=len(str(content).encode("utf-8")),
        mime_type="text/plain",
        lineage_snapshot={
            "task_id": int(task_id),
            "execution_id": str(source_execution_id),
            "tool_name": "e2e.knowledge_seed",
            "artifact_kind": "tool_result",
            "relative_path": target_path.relative_to(workspace_path).as_posix(),
        },
        archive_metadata={
            "source_tool": "e2e.knowledge_seed",
            "evidence_type": "tool_result",
            "seed_mode": "offline_deterministic",
        },
    )
    db.add(evidence)
    db.flush()
    observations = [
        ObservationCreate(
            tenant_id=int(tenant_id),
            user_id=int(user_id),
            engagement_id=int(engagement_id),
            task_id=int(task_id),
            source_execution_id=str(source_execution_id),
            ingestion_run_id=str(ingestion_run_id),
            observation_type="network.host_discovered",
            subject_type="host.ip",
            subject_key=asset_key,
            assertion_level="observed",
            payload={"host_status": "up", "confidence": "high"},
            observation_metadata={
                "source_kind": "deterministic",
                "extractor_family": "e2e.workspace_seed",
                "extractor_version": "1.0",
            },
            observed_at=now,
        ),
        ObservationCreate(
            tenant_id=int(tenant_id),
            user_id=int(user_id),
            engagement_id=int(engagement_id),
            task_id=int(task_id),
            source_execution_id=str(source_execution_id),
            ingestion_run_id=str(ingestion_run_id),
            observation_type="network.service_detected",
            subject_type="service.socket",
            subject_key=service_key,
            assertion_level="observed",
            payload={
                "protocol": "tcp",
                "port": 8443,
                "service_name": "https-alt",
                "product": "deterministic-nginx",
                "version": "1.0",
                "status": "open",
                "confidence": "high",
            },
            observation_metadata={
                "source_kind": "deterministic",
                "extractor_family": "e2e.workspace_seed",
                "extractor_version": "1.0",
            },
            observed_at=now,
        ),
        ObservationCreate(
            tenant_id=int(tenant_id),
            user_id=int(user_id),
            engagement_id=int(engagement_id),
            task_id=int(task_id),
            source_execution_id=str(source_execution_id),
            ingestion_run_id=str(ingestion_run_id),
            observation_type="finding.vulnerability_detected",
            subject_type="finding.vulnerability",
            subject_key=finding_key,
            assertion_level="observed",
            payload={
                "title": str(finding_title),
                "severity": "high",
                "status": "open",
                "confidence": "high",
                "subject_type": "service.socket",
                "subject_key": service_key,
                "detector_id": f"e2e/workspace-{int(task_id)}",
                "source_tool": "e2e.knowledge_seed",
                "evidence_refs": [
                    {
                        "evidence_archive_id": str(evidence_id),
                        "excerpt": str(content),
                    }
                ],
            },
            observation_metadata={
                "source_kind": "deterministic",
                "extractor_family": "e2e.workspace_seed",
                "extractor_version": "1.0",
            },
            observed_at=now,
        ),
        ObservationCreate(
            tenant_id=int(tenant_id),
            user_id=int(user_id),
            engagement_id=int(engagement_id),
            task_id=int(task_id),
            source_execution_id=str(source_execution_id),
            ingestion_run_id=str(ingestion_run_id),
            observation_type="relationship.exposes",
            subject_type="relationship.edge",
            subject_key=f"relationship.edge:{asset_key}:exposes:{service_key}",
            assertion_level="observed",
            payload={
                "source_subject_key": asset_key,
                "relationship_type": "exposes",
                "target_subject_key": service_key,
                "confidence": "high",
            },
            observation_metadata={
                "source_kind": "deterministic",
                "extractor_family": "e2e.workspace_seed",
                "extractor_version": "1.0",
            },
            observed_at=now,
        ),
    ]
    projection = KnowledgeProjectionService(db).project_observations(
        tenant_id=int(tenant_id),
        user_id=int(user_id),
        engagement_id=int(engagement_id),
        observations=observations,
    )
    asset = db.execute(
        select(KnowledgeAsset).where(
            KnowledgeAsset.tenant_id == int(tenant_id),
            KnowledgeAsset.user_id == int(user_id),
            KnowledgeAsset.asset_key == asset_key,
        )
    ).scalar_one()
    service = db.execute(
        select(KnowledgeService).where(
            KnowledgeService.tenant_id == int(tenant_id),
            KnowledgeService.user_id == int(user_id),
            KnowledgeService.service_key == service_key,
        )
    ).scalar_one()
    finding = db.execute(
        select(KnowledgeFinding).where(
            KnowledgeFinding.tenant_id == int(tenant_id),
            KnowledgeFinding.user_id == int(user_id),
            KnowledgeFinding.finding_key == finding_key,
        )
    ).scalar_one()
    for entity_type, entity_id in (
        ("asset", asset.id),
        ("service", service.id),
        ("finding", finding.id),
    ):
        db.add(
            KnowledgeEntityProvenance(
                tenant_id=int(tenant_id),
                user_id=int(user_id),
                entity_type=entity_type,
                entity_id=entity_id,
                engagement_id=int(engagement_id),
                task_id=int(task_id),
                execution_id=source_execution_id,
                tool_name="e2e.knowledge_seed",
                ingestion_run_id=ingestion_run_id,
                observed_at=now,
                confidence="high",
                evidence_archive_id=evidence_id,
            )
        )
    db.flush()
    return {
        "task_id": int(task_id),
        "engagement_id": int(engagement_id),
        "relative_path": target_path.relative_to(workspace_path).as_posix(),
        "finding_key": finding_key,
        "finding_title": str(finding_title),
        "finding_upsert_count": int(projection.finding_upsert_count),
        "asset_id": str(asset.id),
        "service_id": str(service.id),
        "finding_id": str(finding.id),
        "evidence_id": str(evidence.id),
    }


def _seed_reporting_input(
    db: Any,
    *,
    user_id: int,
    tenant_id: int,
    engagement_id: int,
    task_id: int,
) -> dict[str, object]:
    """Seed one current ready reporting memo from task-local durable sources."""
    from datetime import datetime, timezone
    import hashlib

    from backend.models import Task
    from backend.repositories.reporting.task_closure_memo_repository import (
        TaskClosureMemoRepository,
    )
    from backend.services.reporting.evidence_packet_builder import EvidencePacketBuilder
    from backend.services.reporting.knowledge_packet_builder import KnowledgePacketBuilder
    from backend.services.reporting.source_watermark_service import SourceWatermarkService
    from sqlalchemy import select

    task = db.execute(
        select(Task).where(
            Task.id == int(task_id),
            Task.user_id == int(user_id),
            Task.tenant_id == int(tenant_id),
            Task.engagement_id == int(engagement_id),
        )
    ).scalar_one_or_none()
    if task is None:
        raise RuntimeError("Task does not match the requested reporting scope.")

    now = datetime.now(timezone.utc)
    task.status = "stopped"
    task.stopped_at = now
    db.flush()

    knowledge_packet = KnowledgePacketBuilder(db).build_for_task(
        tenant_id=int(tenant_id),
        user_id=int(user_id),
        engagement_id=int(engagement_id),
        task_id=int(task_id),
    )
    evidence_packet = EvidencePacketBuilder(db).build_for_task(
        tenant_id=int(tenant_id),
        user_id=int(user_id),
        engagement_id=int(engagement_id),
        task_id=int(task_id),
    )
    knowledge_ref = next(
        (item.ref for item in knowledge_packet.items if item.record_type == "finding"),
        None,
    )
    evidence_ref = next((item.ref for item in evidence_packet.items), None)
    if knowledge_ref is None or evidence_ref is None:
        raise RuntimeError("Reporting input requires task-local finding and evidence sources.")

    source_watermark = SourceWatermarkService(db).compute_for_task(
        tenant_id=int(tenant_id),
        user_id=int(user_id),
        engagement_id=int(engagement_id),
        task_id=int(task_id),
    )
    memo_payload = {
        "summary": "Deterministic E2E assessment input is ready for reporting.",
        "actions_performed": [{"text": "Collected deterministic task-local evidence."}],
        "reportable_observations": [
            {
                "text": "A deterministic service exposure was observed.",
                "knowledge_refs": [knowledge_ref],
                "evidence_refs": [evidence_ref],
            }
        ],
        "possible_findings": [
            {
                "title": "Deterministic service exposure",
                "severity": "high",
                "confidence": "high",
                "knowledge_refs": [knowledge_ref],
                "evidence_refs": [evidence_ref],
            }
        ],
        "limitations": [],
        "unsupported_notes": [],
        "evidence_refs": [evidence_ref],
        "knowledge_refs": [knowledge_ref],
    }
    repository = TaskClosureMemoRepository(db)
    attempt = repository.create_memo_attempt(
        tenant_id=int(tenant_id),
        user_id=int(user_id),
        created_by_user_id=int(user_id),
        engagement_id=int(engagement_id),
        task_id=int(task_id),
        version=repository.next_memo_version(
            tenant_id=int(tenant_id),
            user_id=int(user_id),
            engagement_id=int(engagement_id),
            task_id=int(task_id),
        ),
        memo_mode="supported",
        source_watermark=source_watermark,
        memo=memo_payload,
        generation_metadata={"source": "offline_deterministic_e2e"},
    )
    memo = repository.mark_memo_ready(
        tenant_id=int(tenant_id),
        user_id=int(user_id),
        engagement_id=int(engagement_id),
        task_id=int(task_id),
        memo_id=attempt.id,
        memo=memo_payload,
        source_watermark=source_watermark,
        generation_metadata={"source": "offline_deterministic_e2e"},
        generated_at=now,
        memo_mode="supported",
    )
    if memo is None:
        raise RuntimeError("Reporting memo could not be promoted to ready.")
    watermark_hash = hashlib.sha256(
        json.dumps(source_watermark, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return {
        "task_id": int(task_id),
        "engagement_id": int(engagement_id),
        "memo_id": str(memo.id),
        "knowledge_ref": str(knowledge_ref),
        "evidence_ref": str(evidence_ref),
        "source_watermark_hash": watermark_hash,
    }


def _seed_usage_settings(
    db: Any,
    *,
    user_id: int,
    tenant_id: int,
    task_id: int,
    conversation_id: str,
) -> dict[str, object]:
    """Seed task-local usage and a masked suite credential through services."""
    from agent.providers.llm.core.identity import OPENAI_PROVIDER_ID
    from backend.models import Task
    from backend.services.llm_provider.credential_service import LLMCredentialService
    from backend.services.usage_tracking.insights_models import UsageRecordMetadata
    from backend.services.usage_tracking.models import (
        CACHE_REPORTING_REPORTED,
        UsageData,
    )
    from backend.services.usage_tracking.service import UsageTrackingService
    from sqlalchemy import select

    task = db.execute(
        select(Task).where(
            Task.id == int(task_id),
            Task.user_id == int(user_id),
            Task.tenant_id == int(tenant_id),
        )
    ).scalar_one_or_none()
    if task is None:
        raise RuntimeError("Task does not match the requested usage scope.")

    seed_rows = (
        (120, 40, 32, str(conversation_id), "planner", "planner"),
        (40, 20, 0, "e2e-usage-filter-control", "finalizer", "finalizer"),
    )
    usage_service = UsageTrackingService(db)
    record_ids: list[int] = []
    for prompt, completion, cached, row_conversation, role, node_name in seed_rows:
        record = usage_service.record_usage(
            task_id=int(task_id),
            user_id=int(user_id),
            usage=UsageData(
                prompt_tokens=prompt,
                completion_tokens=completion,
                total_tokens=prompt + completion,
                model="gpt-5-mini",
                provider=OPENAI_PROVIDER_ID,
                cached_tokens=cached,
                reasoning_tokens=0,
                api_surface="responses",
                cache_reporting=CACHE_REPORTING_REPORTED,
            ),
            source="offline_deterministic_e2e",
            conversation_id=row_conversation,
            usage_metadata=UsageRecordMetadata(
                role=role,
                node_name=node_name,
                execution_branch="normal",
                provider=OPENAI_PROVIDER_ID,
                api_surface="responses",
                request_mode="streaming",
                cache_reporting=CACHE_REPORTING_REPORTED,
                turn_index=len(record_ids),
            ),
        )
        if record is None:
            raise RuntimeError("Usage service rejected a deterministic non-empty row.")
        record_ids.append(int(record.id))

    credential_status = LLMCredentialService(db).upsert_api_key(
        user_id=int(user_id),
        provider=OPENAI_PROVIDER_ID,
        api_key=_usage_settings_credential_secret(user_id=int(user_id), task_id=int(task_id)),
    )
    return {
        "task_id": int(task_id),
        "record_ids": record_ids,
        "call_count": len(record_ids),
        "prompt_tokens": sum(row[0] for row in seed_rows),
        "completion_tokens": sum(row[1] for row in seed_rows),
        "conversation_id": str(conversation_id),
        "credential_masked": bool(
            credential_status.has_api_key and credential_status.masked_api_key
        ),
    }


def _usage_settings_credential_secret(*, user_id: int, task_id: int) -> str:
    """Build a deterministic suite-only value without accepting secret input."""
    return f"sk-e2e-suite-u{int(user_id)}-t{int(task_id)}"


def main(argv: list[str] | None = None) -> int:
    try:
        _require_deterministic_e2e()
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    args = _build_parser().parse_args(argv)
    if str(_REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(_REPO_ROOT))
    try:
        from backend.database import SessionLocal

        db = SessionLocal()
        if args.command == "membership":
            result = _seed_membership_role(
                db,
                actor_user_id=args.actor_user_id,
                target_user_id=args.target_user_id,
                tenant_id=args.tenant_id,
                role=args.role,
            )
        elif args.command == "tenant-membership":
            result = _seed_tenant_membership(
                db,
                user_id=args.user_id,
                tenant_slug=args.tenant_slug,
                tenant_name=args.tenant_name,
                role=args.role,
            )
        elif args.command == "workspace-knowledge":
            result = _seed_workspace_knowledge(
                db,
                user_id=args.user_id,
                tenant_id=args.tenant_id,
                engagement_id=args.engagement_id,
                task_id=args.task_id,
                relative_path=args.relative_path,
                content=args.content,
                finding_title=args.finding_title,
            )
        elif args.command == "reporting-input":
            result = _seed_reporting_input(
                db,
                user_id=args.user_id,
                tenant_id=args.tenant_id,
                engagement_id=args.engagement_id,
                task_id=args.task_id,
            )
        elif args.command == "usage-settings":
            result = _seed_usage_settings(
                db,
                user_id=args.user_id,
                tenant_id=args.tenant_id,
                task_id=args.task_id,
                conversation_id=args.conversation_id,
            )
        else:  # pragma: no cover - argparse enforces the command set.
            raise RuntimeError("Unsupported E2E seed command.")
        db.commit()
    except Exception as exc:
        if "db" in locals():
            db.rollback()
        print(f"Offline E2E seed failed: {type(exc).__name__}.", file=sys.stderr)
        return 1
    finally:
        if "db" in locals():
            db.close()

    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
