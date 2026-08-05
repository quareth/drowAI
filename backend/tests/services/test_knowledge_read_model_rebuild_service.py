"""Tests for the internal read-model rebuild boundary."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import uuid as uuid_lib

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from backend.database import Base
from backend.models.core import Engagement, User
from backend.models.knowledge import (
    EngagementAssetLink,
    EngagementServiceLink,
    EngagementWebPathLink,
    KnowledgeAsset,
    KnowledgeEntityProvenance,
    KnowledgeFinding,
    KnowledgeIngestionRun,
    KnowledgeObservation,
    KnowledgeRelationship,
    KnowledgeService,
    KnowledgeWebPath,
)
from backend.services.knowledge.contracts import IngestionRunCreate, ObservationCreate
from backend.services.knowledge.ingestion_service import KnowledgeIngestionService
from backend.services.knowledge.read_model_rebuild_service import KnowledgeReadModelRebuildService


def _build_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = session_factory()
    db.execute(text("PRAGMA foreign_keys=ON"))
    return engine, db


def _seed_engagement(db, *, username_suffix: str, engagement_name: str) -> Engagement:
    user = User(username=f"execution-plane-rebuild-{username_suffix}-{uuid_lib.uuid4()}", password="secret")
    db.add(user)
    db.flush()
    return _seed_engagement_for_user(db, user_id=user.id, engagement_name=engagement_name)


def _ensure_default_tenant(db) -> int:
    tenant_id = db.execute(text("SELECT id FROM tenants WHERE id = 1")).scalar_one_or_none()
    if tenant_id is None:
        db.execute(
            text(
                """
                INSERT INTO tenants (id, slug, name, status)
                VALUES (1, 'default', 'Default Tenant', 'active')
                """
            )
        )
        return 1
    return int(tenant_id)


def _seed_engagement_for_user(db, *, user_id: int, engagement_name: str) -> Engagement:
    engagement = Engagement(
        user_id=int(user_id),
        tenant_id=int(_ensure_default_tenant(db)),
        name=engagement_name,
        status="active",
    )
    db.add(engagement)
    db.flush()
    return engagement


def _append_observations(
    db,
    *,
    user_id: int,
    engagement_id: int,
    source_execution_id: str,
    observations: list[ObservationCreate],
) -> None:
    ingestion = KnowledgeIngestionService(db)
    run = ingestion.create_or_get_ingestion_run(
        IngestionRunCreate(
            user_id=int(user_id),
            engagement_id=int(engagement_id),
            task_id=None,
            source_execution_id=str(source_execution_id),
            extractor_family="runtime.ingestion.test",
            extractor_version=f"seed-{source_execution_id}",
        )
    )
    normalized = [
        ObservationCreate(
            user_id=int(item.user_id),
            engagement_id=int(item.engagement_id),
            task_id=item.task_id,
            source_execution_id=str(item.source_execution_id),
            ingestion_run_id=str(run.id),
            observation_type=str(item.observation_type),
            subject_type=str(item.subject_type),
            subject_key=str(item.subject_key),
            assertion_level=str(item.assertion_level),
            payload=dict(item.payload or {}),
            observed_at=item.observed_at,
            dedupe_key=item.dedupe_key,
        )
        for item in observations
    ]
    ingestion.insert_observations(
        ingestion_run_id=str(run.id),
        observations=normalized,
    )


def _observation_rows(
    *,
    user_id: int,
    engagement_id: int,
    source_execution_id: str,
    host_ip: str,
    port: int,
    base_time: datetime,
) -> list[ObservationCreate]:
    return [
        ObservationCreate(
            user_id=int(user_id),
            engagement_id=engagement_id,
            task_id=None,
            source_execution_id=source_execution_id,
            ingestion_run_id="seed-placeholder",
            observation_type="network.host_discovered",
            subject_type="host.ip",
            subject_key=f"host.ip:{host_ip}",
            assertion_level="observed",
            payload={"host_status": "up", "confidence": "medium"},
            observed_at=base_time,
        ),
        ObservationCreate(
            user_id=int(user_id),
            engagement_id=engagement_id,
            task_id=None,
            source_execution_id=source_execution_id,
            ingestion_run_id="seed-placeholder",
            observation_type="network.open_port",
            subject_type="service.socket",
            subject_key=f"service.socket:{host_ip}/tcp/{port}",
            assertion_level="observed",
            payload={"confidence": "medium"},
            observed_at=base_time + timedelta(seconds=1),
        ),
    ]


def _web_path_observation_rows(
    *,
    user_id: int,
    engagement_id: int,
    source_execution_id: str,
    canonical_url: str,
    base_time: datetime,
) -> list[ObservationCreate]:
    return [
        ObservationCreate(
            user_id=int(user_id),
            engagement_id=engagement_id,
            task_id=None,
            source_execution_id=source_execution_id,
            ingestion_run_id="seed-placeholder",
            observation_type="web.path_discovered",
            subject_type="web.path",
            subject_key=f"web.path:{canonical_url}",
            assertion_level="observed",
            payload={
                "source": "ffuf",
                "status_code": 200,
                "response_size": 1234,
            },
            observed_at=base_time,
        ),
    ]


def _seed_observation_ledger_for_engagement(
    db,
    *,
    user_id: int,
    engagement_id: int,
    execution_observations: dict[str, list[ObservationCreate]],
) -> None:
    for source_execution_id, rows in execution_observations.items():
        _append_observations(
            db,
            user_id=user_id,
            engagement_id=engagement_id,
            source_execution_id=source_execution_id,
            observations=rows,
        )


def _build_read_models_from_existing_observations(db, *, engagement_id: int) -> None:
    rebuild_service = KnowledgeReadModelRebuildService(db)
    rebuild_service.rebuild_engagement(engagement_id=int(engagement_id))


def _semantic_snapshot(db, *, engagement_id: int) -> dict[str, list[tuple]]:
    assets = db.query(KnowledgeAsset).filter(KnowledgeAsset.engagement_id == int(engagement_id)).all()
    services = db.query(KnowledgeService).filter(KnowledgeService.engagement_id == int(engagement_id)).all()
    findings = db.query(KnowledgeFinding).filter(KnowledgeFinding.engagement_id == int(engagement_id)).all()
    relationships = db.query(KnowledgeRelationship).filter(
        KnowledgeRelationship.engagement_id == int(engagement_id)
    ).all()
    return {
        "assets": sorted(
            (
                row.asset_key,
                row.asset_type,
                row.display_name,
                row.ip_address,
                row.hostname,
                row.status,
                row.first_seen_at,
                row.last_seen_at,
                row.max_confidence,
                int((row.asset_metadata or {}).get("observation_count") or 0),
            )
            for row in assets
        ),
        "services": sorted(
            (
                row.service_key,
                row.protocol,
                row.port,
                row.service_name,
                row.product,
                row.version,
                row.status,
                row.first_seen_at,
                row.last_seen_at,
                int((row.service_metadata or {}).get("observation_count") or 0),
            )
            for row in services
        ),
        "findings": sorted(
            (
                row.finding_key,
                row.finding_type,
                row.subject_type,
                row.subject_key,
                row.title,
                row.severity,
                row.status,
                row.assertion_level,
                row.confidence,
                row.first_seen_at,
                row.last_seen_at,
            )
            for row in findings
        ),
        "relationships": sorted(
            (
                row.relationship_key,
                row.source_subject_key,
                row.relationship_type,
                row.target_subject_key,
                row.confidence,
                row.first_seen_at,
                row.last_seen_at,
            )
            for row in relationships
        ),
    }


def _assert_no_stale_provenance_references(db) -> None:
    model_by_entity_type = {
        "asset": KnowledgeAsset,
        "service": KnowledgeService,
        "finding": KnowledgeFinding,
        "relationship": KnowledgeRelationship,
        "web_path": KnowledgeWebPath,
    }
    rows = db.query(KnowledgeEntityProvenance).all()
    for row in rows:
        model = model_by_entity_type.get(str(row.entity_type))
        if model is None:
            continue
        exists = (
            db.query(model)
            .filter(
                model.id == row.entity_id,
                model.tenant_id == row.tenant_id,
            )
            .count()
            == 1
        )
        assert exists, f"Stale provenance row id={row.id} entity={row.entity_type}:{row.entity_id}"


def test_rebuild_engagement_reconstructs_deleted_read_models_from_observations() -> None:
    engine, db = _build_session()
    try:
        engagement = _seed_engagement(db, username_suffix="reconstruct", engagement_name="Rebuild Reconstruct")
        now = datetime.now(timezone.utc)
        execution_id = str(uuid_lib.uuid4())
        _seed_observation_ledger_for_engagement(
            db,
            user_id=engagement.user_id,
            engagement_id=engagement.id,
            execution_observations={
                execution_id: _observation_rows(
                    user_id=engagement.user_id,
                    engagement_id=engagement.id,
                    source_execution_id=execution_id,
                    host_ip="10.10.10.5",
                    port=443,
                    base_time=now,
                )
            },
        )
        _build_read_models_from_existing_observations(db, engagement_id=engagement.id)

        db.query(KnowledgeRelationship).filter(KnowledgeRelationship.engagement_id == engagement.id).delete()
        db.query(KnowledgeFinding).filter(KnowledgeFinding.engagement_id == engagement.id).delete()
        db.query(KnowledgeService).filter(KnowledgeService.engagement_id == engagement.id).delete()
        db.query(KnowledgeAsset).filter(KnowledgeAsset.engagement_id == engagement.id).delete()
        db.flush()

        result = KnowledgeReadModelRebuildService(db).rebuild_engagement(engagement_id=engagement.id)

        assert result["ok"] is True
        assert result["scope"] == "engagement"
        assert result["observation_count"] == 2
        assert db.query(KnowledgeAsset).filter(KnowledgeAsset.engagement_id == engagement.id).count() == 1
        assert db.query(KnowledgeService).filter(KnowledgeService.engagement_id == engagement.id).count() == 1
    finally:
        db.close()
        engine.dispose()


def test_rebuild_engagement_skips_legacy_application_protocol_service_socket_observations() -> None:
    engine, db = _build_session()
    try:
        engagement = _seed_engagement(db, username_suffix="legacy-service", engagement_name="Legacy Service")
        now = datetime.now(timezone.utc)
        execution_id = str(uuid_lib.uuid4())
        _seed_observation_ledger_for_engagement(
            db,
            user_id=engagement.user_id,
            engagement_id=engagement.id,
            execution_observations={
                execution_id: _observation_rows(
                    user_id=engagement.user_id,
                    engagement_id=engagement.id,
                    source_execution_id=execution_id,
                    host_ip="10.10.10.5",
                    port=21,
                    base_time=now,
                )
            },
        )
        ingestion = db.query(KnowledgeIngestionRun).filter(
            KnowledgeIngestionRun.source_execution_id == execution_id,
        ).one()
        db.add(
            KnowledgeObservation(
                tenant_id=int(engagement.tenant_id),
                user_id=int(engagement.user_id),
                engagement_id=int(engagement.id),
                task_id=None,
                ingestion_run_id=ingestion.id,
                source_execution_id=execution_id,
                observation_type="network.service_observed",
                subject_type="service.socket",
                subject_key="service.socket:10.10.10.5/ftp/21",
                assertion_level="observed",
                payload={"protocol": "ftp", "port": 21, "source": "legacy"},
                observed_at=now + timedelta(seconds=2),
                dedupe_key=str(uuid_lib.uuid4()),
            )
        )
        db.commit()

        result = KnowledgeReadModelRebuildService(db).rebuild_engagement(engagement_id=engagement.id)

        services = db.query(KnowledgeService).filter(KnowledgeService.engagement_id == engagement.id).all()
        assert result["observation_count"] == 2
        assert [row.service_key for row in services] == ["service.socket:10.10.10.5/tcp/21"]
        assert services[0].protocol == "tcp"
    finally:
        db.close()
        engine.dispose()


def test_rebuild_engagement_applies_central_severity_policy_to_ledger_observations() -> None:
    engine, db = _build_session()
    try:
        engagement = _seed_engagement(
            db,
            username_suffix="severity-policy",
            engagement_name="Rebuild Severity Policy",
        )
        now = datetime.now(timezone.utc)
        source_execution_id = str(uuid_lib.uuid4())
        finding_key = "finding.instance:msfconsole:exploit/unix/webapp/drupal_drupalgeddon2:target-192.168.196.16"
        _seed_observation_ledger_for_engagement(
            db,
            user_id=engagement.user_id,
            engagement_id=engagement.id,
            execution_observations={
                source_execution_id: [
                    ObservationCreate(
                        user_id=engagement.user_id,
                        engagement_id=engagement.id,
                        task_id=None,
                        source_execution_id=source_execution_id,
                        ingestion_run_id="seed-placeholder",
                        observation_type="finding.exploit_succeeded",
                        subject_type="finding.instance",
                        subject_key=finding_key,
                        assertion_level="exploited",
                        payload={
                            "source": "msfconsole",
                            "detector_id": "exploit/unix/webapp/drupal_drupalgeddon2",
                            "session_count": 1,
                        },
                        observed_at=now,
                    )
                ],
            },
        )

        result = KnowledgeReadModelRebuildService(db).rebuild_engagement(engagement_id=engagement.id)

        finding = db.query(KnowledgeFinding).filter(
            KnowledgeFinding.engagement_id == engagement.id,
            KnowledgeFinding.finding_key == finding_key,
        ).one()
        assert result["ok"] is True
        assert finding.status == "exploited"
        assert finding.severity == "high"
        assert (finding.finding_metadata or {})["severity_resolution"]["signal"] == (
            "observation_type:finding.exploit_succeeded"
        )
    finally:
        db.close()
        engine.dispose()


def test_rebuild_engagement_clears_stale_provenance_for_rebuilt_entities() -> None:
    engine, db = _build_session()
    try:
        engagement = _seed_engagement(
            db,
            username_suffix="provenance-engagement",
            engagement_name="Rebuild Provenance Engagement",
        )
        now = datetime.now(timezone.utc)
        source_execution_id = str(uuid_lib.uuid4())
        _seed_observation_ledger_for_engagement(
            db,
            user_id=engagement.user_id,
            engagement_id=engagement.id,
            execution_observations={
                source_execution_id: _observation_rows(
                    user_id=engagement.user_id,
                    engagement_id=engagement.id,
                    source_execution_id=source_execution_id,
                    host_ip="10.10.90.1",
                    port=443,
                    base_time=now,
                )
            },
        )

        rebuild = KnowledgeReadModelRebuildService(db)
        rebuild.rebuild_engagement(engagement_id=engagement.id)

        asset = db.query(KnowledgeAsset).filter(
            KnowledgeAsset.engagement_id == engagement.id,
            KnowledgeAsset.asset_key == "host.ip:10.10.90.1",
        ).one()
        service = db.query(KnowledgeService).filter(
            KnowledgeService.engagement_id == engagement.id,
            KnowledgeService.service_key == "service.socket:10.10.90.1/tcp/443",
        ).one()
        db.add_all(
            [
                KnowledgeEntityProvenance(
                    tenant_id=int(engagement.tenant_id),
                    user_id=int(engagement.user_id),
                    entity_type="asset",
                    entity_id=asset.id,
                    engagement_id=engagement.id,
                    execution_id=source_execution_id,
                    observed_at=now,
                ),
                KnowledgeEntityProvenance(
                    tenant_id=int(engagement.tenant_id),
                    user_id=int(engagement.user_id),
                    entity_type="service",
                    entity_id=service.id,
                    engagement_id=engagement.id,
                    execution_id=source_execution_id,
                    observed_at=now + timedelta(seconds=1),
                ),
            ]
        )
        db.flush()

        rebuild.rebuild_engagement(engagement_id=engagement.id)

        assert (
            db.query(KnowledgeEntityProvenance)
            .filter(
                KnowledgeEntityProvenance.tenant_id == int(engagement.tenant_id),
                KnowledgeEntityProvenance.entity_type.in_(["asset", "service"]),
            )
            .count()
            == 0
        )
        _assert_no_stale_provenance_references(db)
    finally:
        db.close()
        engine.dispose()


def test_rebuild_engagement_is_idempotent_for_semantic_state() -> None:
    engine, db = _build_session()
    try:
        engagement = _seed_engagement(db, username_suffix="idempotent", engagement_name="Rebuild Idempotent")
        now = datetime.now(timezone.utc)
        exec_one = str(uuid_lib.uuid4())
        exec_two = str(uuid_lib.uuid4())
        _seed_observation_ledger_for_engagement(
            db,
            user_id=engagement.user_id,
            engagement_id=engagement.id,
            execution_observations={
                exec_one: _observation_rows(
                    user_id=engagement.user_id,
                    engagement_id=engagement.id,
                    source_execution_id=exec_one,
                    host_ip="10.10.20.1",
                    port=22,
                    base_time=now,
                ),
                exec_two: _observation_rows(
                    user_id=engagement.user_id,
                    engagement_id=engagement.id,
                    source_execution_id=exec_two,
                    host_ip="10.10.20.2",
                    port=80,
                    base_time=now + timedelta(minutes=1),
                ),
            },
        )
        service = KnowledgeReadModelRebuildService(db)

        first = service.rebuild_engagement(engagement_id=engagement.id)
        first_snapshot = _semantic_snapshot(db, engagement_id=engagement.id)
        second = service.rebuild_engagement(engagement_id=engagement.id)
        second_snapshot = _semantic_snapshot(db, engagement_id=engagement.id)

        assert first["ok"] is True
        assert second["ok"] is True
        assert first_snapshot == second_snapshot
    finally:
        db.close()
        engine.dispose()


def test_rebuild_source_execution_clears_stale_provenance_for_impacted_identities_only() -> None:
    engine, db = _build_session()
    try:
        engagement = _seed_engagement(
            db,
            username_suffix="provenance-source",
            engagement_name="Rebuild Provenance Source",
        )
        now = datetime.now(timezone.utc)
        exec_one = str(uuid_lib.uuid4())
        exec_two = str(uuid_lib.uuid4())
        _seed_observation_ledger_for_engagement(
            db,
            user_id=engagement.user_id,
            engagement_id=engagement.id,
            execution_observations={
                exec_one: _observation_rows(
                    user_id=engagement.user_id,
                    engagement_id=engagement.id,
                    source_execution_id=exec_one,
                    host_ip="10.10.91.1",
                    port=443,
                    base_time=now,
                ),
                exec_two: _observation_rows(
                    user_id=engagement.user_id,
                    engagement_id=engagement.id,
                    source_execution_id=exec_two,
                    host_ip="10.10.91.2",
                    port=8443,
                    base_time=now + timedelta(minutes=1),
                ),
            },
        )

        rebuild = KnowledgeReadModelRebuildService(db)
        rebuild.rebuild_engagement(engagement_id=engagement.id)

        asset_one = db.query(KnowledgeAsset).filter(
            KnowledgeAsset.engagement_id == engagement.id,
            KnowledgeAsset.asset_key == "host.ip:10.10.91.1",
        ).one()
        asset_two = db.query(KnowledgeAsset).filter(
            KnowledgeAsset.engagement_id == engagement.id,
            KnowledgeAsset.asset_key == "host.ip:10.10.91.2",
        ).one()
        service_one = db.query(KnowledgeService).filter(
            KnowledgeService.engagement_id == engagement.id,
            KnowledgeService.service_key == "service.socket:10.10.91.1/tcp/443",
        ).one()
        service_two = db.query(KnowledgeService).filter(
            KnowledgeService.engagement_id == engagement.id,
            KnowledgeService.service_key == "service.socket:10.10.91.2/tcp/8443",
        ).one()

        db.add_all(
            [
                KnowledgeEntityProvenance(
                    tenant_id=int(engagement.tenant_id),
                    user_id=int(engagement.user_id),
                    entity_type="asset",
                    entity_id=asset_one.id,
                    engagement_id=engagement.id,
                    execution_id=exec_one,
                    observed_at=now,
                ),
                KnowledgeEntityProvenance(
                    tenant_id=int(engagement.tenant_id),
                    user_id=int(engagement.user_id),
                    entity_type="asset",
                    entity_id=asset_two.id,
                    engagement_id=engagement.id,
                    execution_id=exec_two,
                    observed_at=now + timedelta(minutes=1),
                ),
                KnowledgeEntityProvenance(
                    tenant_id=int(engagement.tenant_id),
                    user_id=int(engagement.user_id),
                    entity_type="service",
                    entity_id=service_one.id,
                    engagement_id=engagement.id,
                    execution_id=exec_one,
                    observed_at=now + timedelta(seconds=1),
                ),
                KnowledgeEntityProvenance(
                    tenant_id=int(engagement.tenant_id),
                    user_id=int(engagement.user_id),
                    entity_type="service",
                    entity_id=service_two.id,
                    engagement_id=engagement.id,
                    execution_id=exec_two,
                    observed_at=now + timedelta(minutes=1, seconds=1),
                ),
            ]
        )
        db.flush()

        rebuild.rebuild_source_execution(
            source_execution_id=exec_two,
            engagement_id=engagement.id,
        )

        assert (
            db.query(KnowledgeEntityProvenance)
            .filter(
                KnowledgeEntityProvenance.entity_type == "asset",
                KnowledgeEntityProvenance.entity_id == asset_one.id,
            )
            .count()
            == 1
        )
        assert (
            db.query(KnowledgeEntityProvenance)
            .filter(
                KnowledgeEntityProvenance.entity_type == "service",
                KnowledgeEntityProvenance.entity_id == service_one.id,
            )
            .count()
            == 1
        )
        assert (
            db.query(KnowledgeEntityProvenance)
            .filter(
                KnowledgeEntityProvenance.entity_id.in_([asset_two.id, service_two.id]),
            )
            .count()
            == 0
        )
        _assert_no_stale_provenance_references(db)
    finally:
        db.close()
        engine.dispose()


def test_rebuild_source_execution_rebuilds_impacted_identities_only() -> None:
    engine, db = _build_session()
    try:
        engagement = _seed_engagement(db, username_suffix="source", engagement_name="Rebuild Source Scope")
        now = datetime.now(timezone.utc)
        exec_one = str(uuid_lib.uuid4())
        exec_two = str(uuid_lib.uuid4())
        _seed_observation_ledger_for_engagement(
            db,
            user_id=engagement.user_id,
            engagement_id=engagement.id,
            execution_observations={
                exec_one: _observation_rows(
                    user_id=engagement.user_id,
                    engagement_id=engagement.id,
                    source_execution_id=exec_one,
                    host_ip="10.10.30.1",
                    port=443,
                    base_time=now,
                ),
                exec_two: _observation_rows(
                    user_id=engagement.user_id,
                    engagement_id=engagement.id,
                    source_execution_id=exec_two,
                    host_ip="10.10.30.2",
                    port=8080,
                    base_time=now + timedelta(minutes=1),
                ),
            },
        )
        rebuild = KnowledgeReadModelRebuildService(db)
        rebuild.rebuild_engagement(engagement_id=engagement.id)

        db.query(KnowledgeService).filter(
            KnowledgeService.engagement_id == engagement.id,
            KnowledgeService.service_key == "service.socket:10.10.30.2/tcp/8080",
        ).delete()
        db.query(KnowledgeAsset).filter(
            KnowledgeAsset.engagement_id == engagement.id,
            KnowledgeAsset.asset_key == "host.ip:10.10.30.2",
        ).delete()
        db.flush()

        result = rebuild.rebuild_source_execution(
            source_execution_id=exec_two,
            engagement_id=engagement.id,
        )

        assert result["ok"] is True
        assert result["scope"] == "source_execution"
        assert result["source_execution_id"] == exec_two
        assert db.query(KnowledgeAsset).filter(
            KnowledgeAsset.engagement_id == engagement.id,
            KnowledgeAsset.asset_key == "host.ip:10.10.30.1",
        ).count() == 1
        assert db.query(KnowledgeAsset).filter(
            KnowledgeAsset.engagement_id == engagement.id,
            KnowledgeAsset.asset_key == "host.ip:10.10.30.2",
        ).count() == 1
        assert db.query(KnowledgeService).filter(
            KnowledgeService.engagement_id == engagement.id,
            KnowledgeService.service_key == "service.socket:10.10.30.2/tcp/8080",
        ).count() == 1
    finally:
        db.close()
        engine.dispose()


def test_rebuild_engagement_restores_deleted_web_paths_when_feature_enabled() -> None:
    engine, db = _build_session()
    try:
        engagement = _seed_engagement(
            db,
            username_suffix="web-reconstruct",
            engagement_name="Rebuild Web Reconstruct",
        )
        now = datetime.now(timezone.utc)
        source_execution_id = str(uuid_lib.uuid4())
        _seed_observation_ledger_for_engagement(
            db,
            user_id=engagement.user_id,
            engagement_id=engagement.id,
            execution_observations={
                source_execution_id: [
                    *_observation_rows(
                        user_id=engagement.user_id,
                        engagement_id=engagement.id,
                        source_execution_id=source_execution_id,
                        host_ip="10.10.35.1",
                        port=443,
                        base_time=now,
                    ),
                    *_web_path_observation_rows(
                        user_id=engagement.user_id,
                        engagement_id=engagement.id,
                        source_execution_id=source_execution_id,
                        canonical_url="https://10.10.35.1/admin",
                        base_time=now + timedelta(seconds=2),
                    ),
                ]
            },
        )

        service = KnowledgeReadModelRebuildService(db)
        first = service.rebuild_engagement(engagement_id=engagement.id)
        assert first["web_path_upsert_count"] == 1
        assert first["web_path_insert_count"] == 1

        db.query(EngagementWebPathLink).filter(
            EngagementWebPathLink.engagement_id == engagement.id
        ).delete()
        db.query(KnowledgeWebPath).filter(
            KnowledgeWebPath.user_id == engagement.user_id
        ).delete()
        db.flush()

        rebuilt = service.rebuild_engagement(engagement_id=engagement.id)
        assert rebuilt["ok"] is True
        assert rebuilt["web_path_upsert_count"] == 1
        assert rebuilt["web_path_insert_count"] == 1
        assert db.query(KnowledgeWebPath).filter(
            KnowledgeWebPath.user_id == engagement.user_id
        ).count() == 1
        assert db.query(EngagementWebPathLink).filter(
            EngagementWebPathLink.engagement_id == engagement.id
        ).count() == 1
    finally:
        db.close()
        engine.dispose()


def test_rebuild_source_execution_restores_impacted_web_paths_only() -> None:
    engine, db = _build_session()
    try:
        engagement = _seed_engagement(
            db,
            username_suffix="web-source",
            engagement_name="Rebuild Web Source Scope",
        )
        now = datetime.now(timezone.utc)
        exec_one = str(uuid_lib.uuid4())
        exec_two = str(uuid_lib.uuid4())

        _seed_observation_ledger_for_engagement(
            db,
            user_id=engagement.user_id,
            engagement_id=engagement.id,
            execution_observations={
                exec_one: [
                    *_observation_rows(
                        user_id=engagement.user_id,
                        engagement_id=engagement.id,
                        source_execution_id=exec_one,
                        host_ip="10.10.36.1",
                        port=443,
                        base_time=now,
                    ),
                    *_web_path_observation_rows(
                        user_id=engagement.user_id,
                        engagement_id=engagement.id,
                        source_execution_id=exec_one,
                        canonical_url="https://10.10.36.1/",
                        base_time=now + timedelta(seconds=2),
                    ),
                ],
                exec_two: [
                    *_observation_rows(
                        user_id=engagement.user_id,
                        engagement_id=engagement.id,
                        source_execution_id=exec_two,
                        host_ip="10.10.36.1",
                        port=443,
                        base_time=now + timedelta(minutes=1),
                    ),
                    *_web_path_observation_rows(
                        user_id=engagement.user_id,
                        engagement_id=engagement.id,
                        source_execution_id=exec_two,
                        canonical_url="https://10.10.36.1/admin",
                        base_time=now + timedelta(minutes=1, seconds=2),
                    ),
                ],
            },
        )

        rebuild = KnowledgeReadModelRebuildService(db)
        rebuild.rebuild_engagement(engagement_id=engagement.id)
        assert db.query(KnowledgeWebPath).filter(
            KnowledgeWebPath.user_id == engagement.user_id
        ).count() == 2

        db.query(EngagementWebPathLink).filter(
            EngagementWebPathLink.engagement_id == engagement.id,
            EngagementWebPathLink.web_path_id.in_(
                db.query(KnowledgeWebPath.id).filter(
                    KnowledgeWebPath.user_id == engagement.user_id,
                    KnowledgeWebPath.canonical_url == "https://10.10.36.1/admin",
                )
            ),
        ).delete(synchronize_session=False)
        db.query(KnowledgeWebPath).filter(
            KnowledgeWebPath.user_id == engagement.user_id,
            KnowledgeWebPath.canonical_url == "https://10.10.36.1/admin",
        ).delete(synchronize_session=False)
        db.flush()

        result = rebuild.rebuild_source_execution(
            source_execution_id=exec_two,
            engagement_id=engagement.id,
        )
        assert result["ok"] is True
        assert result["scope"] == "source_execution"
        assert result["web_path_upsert_count"] == 1
        assert result["web_path_insert_count"] == 1
        assert db.query(KnowledgeWebPath).filter(
            KnowledgeWebPath.user_id == engagement.user_id,
            KnowledgeWebPath.canonical_url == "https://10.10.36.1/",
        ).count() == 1
        assert db.query(KnowledgeWebPath).filter(
            KnowledgeWebPath.user_id == engagement.user_id,
            KnowledgeWebPath.canonical_url == "https://10.10.36.1/admin",
        ).count() == 1
    finally:
        db.close()
        engine.dispose()


def test_rebuild_engagement_scope_does_not_touch_other_engagement() -> None:
    engine, db = _build_session()
    try:
        engagement_a = _seed_engagement(db, username_suffix="scope-a", engagement_name="Rebuild Scope A")
        engagement_b = _seed_engagement(db, username_suffix="scope-b", engagement_name="Rebuild Scope B")
        now = datetime.now(timezone.utc)
        exec_a = str(uuid_lib.uuid4())
        exec_b = str(uuid_lib.uuid4())

        _seed_observation_ledger_for_engagement(
            db,
            user_id=engagement_a.user_id,
            engagement_id=engagement_a.id,
            execution_observations={
                exec_a: _observation_rows(
                    user_id=engagement_a.user_id,
                    engagement_id=engagement_a.id,
                    source_execution_id=exec_a,
                    host_ip="10.10.40.1",
                    port=8443,
                    base_time=now,
                )
            },
        )
        _seed_observation_ledger_for_engagement(
            db,
            user_id=engagement_b.user_id,
            engagement_id=engagement_b.id,
            execution_observations={
                exec_b: _observation_rows(
                    user_id=engagement_b.user_id,
                    engagement_id=engagement_b.id,
                    source_execution_id=exec_b,
                    host_ip="10.10.50.1",
                    port=21,
                    base_time=now + timedelta(minutes=1),
                )
            },
        )
        rebuild = KnowledgeReadModelRebuildService(db)
        rebuild.rebuild_engagement(engagement_id=engagement_a.id)
        rebuild.rebuild_engagement(engagement_id=engagement_b.id)
        before_b = _semantic_snapshot(db, engagement_id=engagement_b.id)

        rebuild.rebuild_engagement(engagement_id=engagement_a.id)
        after_b = _semantic_snapshot(db, engagement_id=engagement_b.id)

        assert before_b == after_b
    finally:
        db.close()
        engine.dispose()


def test_rebuild_engagement_replays_all_same_user_engagements_and_preserves_links() -> None:
    engine, db = _build_session()
    try:
        user = User(username=f"execution-plane-rebuild-shared-{uuid_lib.uuid4()}", password="secret")
        db.add(user)
        db.flush()
        engagement_a = _seed_engagement_for_user(
            db,
            user_id=user.id,
            engagement_name="Rebuild Shared Scope A",
        )
        engagement_b = _seed_engagement_for_user(
            db,
            user_id=user.id,
            engagement_name="Rebuild Shared Scope B",
        )
        now = datetime.now(timezone.utc)
        exec_a = str(uuid_lib.uuid4())
        exec_b = str(uuid_lib.uuid4())

        _seed_observation_ledger_for_engagement(
            db,
            user_id=user.id,
            engagement_id=engagement_a.id,
            execution_observations={
                exec_a: _observation_rows(
                    user_id=user.id,
                    engagement_id=engagement_a.id,
                    source_execution_id=exec_a,
                    host_ip="10.10.70.1",
                    port=443,
                    base_time=now,
                )
            },
        )
        _seed_observation_ledger_for_engagement(
            db,
            user_id=user.id,
            engagement_id=engagement_b.id,
            execution_observations={
                exec_b: _observation_rows(
                    user_id=user.id,
                    engagement_id=engagement_b.id,
                    source_execution_id=exec_b,
                    host_ip="10.10.70.1",
                    port=443,
                    base_time=now + timedelta(minutes=1),
                )
            },
        )

        rebuild = KnowledgeReadModelRebuildService(db)
        result = rebuild.rebuild_engagement(engagement_id=engagement_a.id)

        assert result["ok"] is True
        assert sorted(result.get("replayed_engagement_ids") or []) == sorted(
            [engagement_a.id, engagement_b.id]
        )
        assert db.query(KnowledgeAsset).filter(
            KnowledgeAsset.user_id == user.id,
            KnowledgeAsset.asset_key == "host.ip:10.10.70.1",
        ).count() == 1
        assert db.query(KnowledgeService).filter(
            KnowledgeService.user_id == user.id,
            KnowledgeService.service_key == "service.socket:10.10.70.1/tcp/443",
        ).count() == 1
        assert db.query(EngagementAssetLink).filter(
            EngagementAssetLink.engagement_id == engagement_a.id
        ).count() == 1
        assert db.query(EngagementAssetLink).filter(
            EngagementAssetLink.engagement_id == engagement_b.id
        ).count() == 1
        assert db.query(EngagementServiceLink).filter(
            EngagementServiceLink.engagement_id == engagement_a.id
        ).count() == 1
        assert db.query(EngagementServiceLink).filter(
            EngagementServiceLink.engagement_id == engagement_b.id
        ).count() == 1
    finally:
        db.close()
        engine.dispose()


def test_rebuild_source_execution_preserves_sibling_engagement_links_for_shared_web_path() -> None:
    engine, db = _build_session()
    try:
        user = User(username=f"execution-plane-rebuild-shared-source-{uuid_lib.uuid4()}", password="secret")
        db.add(user)
        db.flush()
        engagement_a = _seed_engagement_for_user(
            db,
            user_id=user.id,
            engagement_name="Rebuild Shared Source A",
        )
        engagement_b = _seed_engagement_for_user(
            db,
            user_id=user.id,
            engagement_name="Rebuild Shared Source B",
        )
        now = datetime.now(timezone.utc)
        exec_a = str(uuid_lib.uuid4())
        exec_b = str(uuid_lib.uuid4())

        _seed_observation_ledger_for_engagement(
            db,
            user_id=user.id,
            engagement_id=engagement_a.id,
            execution_observations={
                exec_a: [
                    *_observation_rows(
                        user_id=user.id,
                        engagement_id=engagement_a.id,
                        source_execution_id=exec_a,
                        host_ip="10.10.80.1",
                        port=443,
                        base_time=now,
                    ),
                    *_web_path_observation_rows(
                        user_id=user.id,
                        engagement_id=engagement_a.id,
                        source_execution_id=exec_a,
                        canonical_url="https://10.10.80.1/admin",
                        base_time=now + timedelta(seconds=2),
                    ),
                ]
            },
        )
        _seed_observation_ledger_for_engagement(
            db,
            user_id=user.id,
            engagement_id=engagement_b.id,
            execution_observations={
                exec_b: [
                    *_observation_rows(
                        user_id=user.id,
                        engagement_id=engagement_b.id,
                        source_execution_id=exec_b,
                        host_ip="10.10.80.1",
                        port=443,
                        base_time=now + timedelta(minutes=1),
                    ),
                    *_web_path_observation_rows(
                        user_id=user.id,
                        engagement_id=engagement_b.id,
                        source_execution_id=exec_b,
                        canonical_url="https://10.10.80.1/admin",
                        base_time=now + timedelta(minutes=1, seconds=2),
                    ),
                ]
            },
        )

        rebuild = KnowledgeReadModelRebuildService(db)
        rebuild.rebuild_engagement(engagement_id=engagement_a.id)

        result = rebuild.rebuild_source_execution(
            source_execution_id=exec_a,
            engagement_id=engagement_a.id,
        )

        assert result["ok"] is True
        assert sorted(result.get("replayed_engagement_ids") or []) == sorted(
            [engagement_a.id, engagement_b.id]
        )
        assert db.query(EngagementServiceLink).filter(
            EngagementServiceLink.engagement_id == engagement_a.id
        ).count() == 1
        assert db.query(EngagementServiceLink).filter(
            EngagementServiceLink.engagement_id == engagement_b.id
        ).count() == 1
        assert db.query(EngagementWebPathLink).filter(
            EngagementWebPathLink.engagement_id == engagement_a.id
        ).count() == 1
        assert db.query(EngagementWebPathLink).filter(
            EngagementWebPathLink.engagement_id == engagement_b.id
        ).count() == 1
    finally:
        db.close()
        engine.dispose()


def test_rebuild_source_execution_raises_when_no_matching_observations() -> None:
    engine, db = _build_session()
    try:
        engagement = _seed_engagement(db, username_suffix="missing", engagement_name="Rebuild Missing")
        source_execution_id = str(uuid_lib.uuid4())
        run = KnowledgeIngestionRun(
            tenant_id=int(engagement.tenant_id),
            user_id=engagement.user_id,
            engagement_id=engagement.id,
            task_id=None,
            source_execution_id=source_execution_id,
            extractor_family="runtime.ingestion.test",
            extractor_version="seed-missing",
            status="succeeded",
            run_metadata={},
        )
        db.add(run)
        db.flush()
        assert db.query(KnowledgeObservation).filter(
            KnowledgeObservation.source_execution_id == source_execution_id
        ).count() == 0

        try:
            KnowledgeReadModelRebuildService(db).rebuild_source_execution(
                source_execution_id=source_execution_id,
                engagement_id=engagement.id,
            )
            assert False, "Expected rebuild_source_execution to fail without observations"
        except ValueError as exc:
            assert "No observations found" in str(exc)
    finally:
        db.close()
        engine.dispose()
