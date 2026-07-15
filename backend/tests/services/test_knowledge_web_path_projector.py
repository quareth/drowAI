"""Tests for deterministic `WebPathProjector` canonical web-path upserts."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from functools import partial
import uuid as uuid_lib

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from backend.database import Base
from backend.models.core import Engagement, User
from backend.models.knowledge import (
    EngagementWebPathLink,
    KnowledgeAsset,
    KnowledgeService,
    KnowledgeWebPath,
)
from backend.services.knowledge.contracts import ObservationCreate as _ObservationCreate
from backend.services.knowledge.projection.engagement_link_projector import EngagementLinkProjector
from backend.services.knowledge.projection.web_path_projector import WebPathProjector

ObservationCreate = partial(_ObservationCreate, user_id=1)


def _build_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = session_factory()
    db.execute(text("PRAGMA foreign_keys=ON"))
    return engine, db


def _seed_engagement(db):
    db.execute(
        text(
            "INSERT OR IGNORE INTO tenants (id, slug, name, created_at) "
            "VALUES (1, 'tenant-1', 'Tenant 1', CURRENT_TIMESTAMP)"
        )
    )
    user = User(username=f"web-path-projector-{uuid_lib.uuid4()}", password="secret")
    db.add(user)
    db.flush()
    engagement = Engagement(user_id=user.id, tenant_id=1, name="Web Path Projection", status="active")
    db.add(engagement)
    db.flush()
    return engagement


def test_web_path_projector_upserts_one_row_per_user_and_canonical_url() -> None:
    engine, db = _build_session()
    try:
        engagement = _seed_engagement(db)
        projector = WebPathProjector()
        now = datetime.now(timezone.utc)
        observations = [
            ObservationCreate(
                engagement_id=engagement.id,
                task_id=None,
                source_execution_id="exec-path-1",
                ingestion_run_id="run-path-1",
                observation_type="web.path_discovered",
                subject_type="web.path",
                subject_key="web.path:http://10.10.10.8/admin",
                assertion_level="observed",
                payload={"source": "ffuf", "status_code": 200, "response_size": 420},
                observed_at=now,
            ),
            ObservationCreate(
                engagement_id=engagement.id,
                task_id=None,
                source_execution_id="exec-path-2",
                ingestion_run_id="run-path-2",
                observation_type="web.path_discovered",
                subject_type="web.path",
                subject_key="web.path:http://10.10.10.8/admin",
                assertion_level="observed",
                payload={"source": "gobuster", "status_code": 403, "response_size": 432},
                observed_at=now + timedelta(minutes=2),
            ),
        ]

        counts = projector.upsert_from_observations(
            db=db,
            user_id=engagement.user_id,
            engagement_id=engagement.id,
            observations=observations,
            asset_key_to_id={},
            service_key_to_id={},
            tenant_id=engagement.tenant_id,
        )

        rows = db.query(KnowledgeWebPath).filter(KnowledgeWebPath.user_id == engagement.user_id).all()
        assert len(rows) == 1
        row = rows[0]
        assert counts.upsert_count == 1
        assert counts.insert_count == 1
        assert row.canonical_url == "http://10.10.10.8/admin"
        assert row.origin_key == "http://10.10.10.8"
        assert row.path == "/admin"
        assert row.producer_summary["ffuf"]["seen_count"] == 1
        assert row.producer_summary["gobuster"]["seen_count"] == 1
        assert row.last_status_code == 403
    finally:
        db.close()
        engine.dispose()


def test_web_path_projector_keeps_calibrated_baseline_true_once_set() -> None:
    engine, db = _build_session()
    try:
        engagement = _seed_engagement(db)
        projector = WebPathProjector()
        now = datetime.now(timezone.utc)
        initial = ObservationCreate(
            engagement_id=engagement.id,
            task_id=None,
            source_execution_id="exec-cal-1",
            ingestion_run_id="run-cal-1",
            observation_type="web.path_discovered",
            subject_type="web.path",
            subject_key="web.path:https://example.com/login",
            assertion_level="observed",
            payload={"source": "ffuf", "status_code": 200, "calibrated": True},
            observed_at=now,
        )
        demotion_attempt = ObservationCreate(
            engagement_id=engagement.id,
            task_id=None,
            source_execution_id="exec-cal-2",
            ingestion_run_id="run-cal-2",
            observation_type="web.path_discovered",
            subject_type="web.path",
            subject_key="web.path:https://example.com/login",
            assertion_level="observed",
            payload={"source": "ffuf", "status_code": 200, "calibrated": False},
            observed_at=now + timedelta(minutes=10),
        )

        projector.upsert_from_observations(
            db=db,
            user_id=engagement.user_id,
            engagement_id=engagement.id,
            observations=[initial],
            asset_key_to_id={},
            service_key_to_id={},
            tenant_id=engagement.tenant_id,
        )
        projector.upsert_from_observations(
            db=db,
            user_id=engagement.user_id,
            engagement_id=engagement.id,
            observations=[demotion_attempt],
            asset_key_to_id={},
            service_key_to_id={},
            tenant_id=engagement.tenant_id,
        )

        row = db.query(KnowledgeWebPath).filter(KnowledgeWebPath.user_id == engagement.user_id).one()
        assert row.calibrated_baseline is True
    finally:
        db.close()
        engine.dispose()


def test_web_path_projector_resolves_service_and_asset_when_available_without_synthesizing() -> None:
    engine, db = _build_session()
    try:
        engagement = _seed_engagement(db)
        projector = WebPathProjector()
        now = datetime.now(timezone.utc)
        observation = ObservationCreate(
            engagement_id=engagement.id,
            task_id=None,
            source_execution_id="exec-link-1",
            ingestion_run_id="run-link-1",
            observation_type="web.path_discovered",
            subject_type="web.path",
            subject_key="web.path:http://10.10.10.9/admin",
            assertion_level="observed",
            payload={"source": "ffuf", "status_code": 200},
            observed_at=now,
        )

        projector.upsert_from_observations(
            db=db,
            user_id=engagement.user_id,
            engagement_id=engagement.id,
            observations=[observation],
            asset_key_to_id={},
            service_key_to_id={},
            tenant_id=engagement.tenant_id,
        )
        row = db.query(KnowledgeWebPath).filter(KnowledgeWebPath.user_id == engagement.user_id).one()
        assert row.asset_id is None
        assert row.service_id is None
        assert db.query(KnowledgeAsset).count() == 0
        assert db.query(KnowledgeService).count() == 0

        asset = KnowledgeAsset(
            tenant_id=engagement.tenant_id,
            user_id=engagement.user_id,
            engagement_id=engagement.id,
            asset_key="host.ip:10.10.10.9",
            asset_type="host.ip",
            display_name="10.10.10.9",
            first_seen_at=now,
            last_seen_at=now,
            asset_metadata={},
        )
        db.add(asset)
        db.flush()
        service = KnowledgeService(
            tenant_id=engagement.tenant_id,
            user_id=engagement.user_id,
            engagement_id=engagement.id,
            service_key="service.socket:10.10.10.9/tcp/80",
            asset_id=asset.id,
            protocol="tcp",
            port=80,
            first_seen_at=now,
            last_seen_at=now,
            service_metadata={},
        )
        db.add(service)
        db.flush()

        projector.upsert_from_observations(
            db=db,
            user_id=engagement.user_id,
            engagement_id=engagement.id,
            observations=[observation],
            asset_key_to_id={"host.ip:10.10.10.9": str(asset.id)},
            service_key_to_id={"service.socket:10.10.10.9/tcp/80": str(service.id)},
            tenant_id=engagement.tenant_id,
        )
        refreshed = db.query(KnowledgeWebPath).filter(KnowledgeWebPath.user_id == engagement.user_id).one()
        assert str(refreshed.service_id) == str(service.id)
        assert str(refreshed.asset_id) == str(asset.id)
    finally:
        db.close()
        engine.dispose()


def test_web_path_projector_noise_score_is_deterministic_and_bounded() -> None:
    engine, db = _build_session()
    try:
        engagement = _seed_engagement(db)
        projector = WebPathProjector()
        now = datetime.now(timezone.utc)
        observations = [
            ObservationCreate(
                engagement_id=engagement.id,
                task_id=None,
                source_execution_id="exec-noise-1",
                ingestion_run_id="run-noise-1",
                observation_type="web.path_discovered",
                subject_type="web.path",
                subject_key="web.path:https://example.com/private",
                assertion_level="observed",
                payload={"source": "ffuf", "status_code": 401, "calibrated": True},
                observed_at=now + timedelta(seconds=2),
            ),
            ObservationCreate(
                engagement_id=engagement.id,
                task_id=None,
                source_execution_id="exec-noise-2",
                ingestion_run_id="run-noise-1",
                observation_type="web.path_discovered",
                subject_type="web.path",
                subject_key="web.path:https://example.com/private",
                assertion_level="observed",
                payload={"source": "gobuster", "status_code": 302},
                observed_at=now,
            ),
        ]

        projector.upsert_from_observations(
            db=db,
            user_id=engagement.user_id,
            engagement_id=engagement.id,
            observations=observations,
            asset_key_to_id={},
            service_key_to_id={},
            tenant_id=engagement.tenant_id,
        )
        first_score = db.query(KnowledgeWebPath.noise_score).scalar()

        db.query(KnowledgeWebPath).delete()
        db.flush()

        projector.upsert_from_observations(
            db=db,
            user_id=engagement.user_id,
            engagement_id=engagement.id,
            observations=list(reversed(observations)),
            asset_key_to_id={},
            service_key_to_id={},
            tenant_id=engagement.tenant_id,
        )
        second_score = db.query(KnowledgeWebPath.noise_score).scalar()

        assert first_score == second_score
        assert 0.0 <= float(first_score) <= 1.0
    finally:
        db.close()
        engine.dispose()


def test_engagement_web_path_link_upsert_is_idempotent_and_tracks_first_last_seen() -> None:
    engine, db = _build_session()
    try:
        engagement = _seed_engagement(db)
        web_projector = WebPathProjector()
        link_projector = EngagementLinkProjector()
        now = datetime.now(timezone.utc)
        observations = [
            ObservationCreate(
                engagement_id=engagement.id,
                task_id=None,
                source_execution_id="exec-link-upsert-1",
                ingestion_run_id="run-link-upsert-1",
                observation_type="web.path_discovered",
                subject_type="web.path",
                subject_key="web.path:http://10.10.10.10/admin",
                assertion_level="observed",
                payload={"source": "ffuf", "status_code": 200},
                observed_at=now,
            ),
            ObservationCreate(
                engagement_id=engagement.id,
                task_id=None,
                source_execution_id="exec-link-upsert-2",
                ingestion_run_id="run-link-upsert-2",
                observation_type="web.path_discovered",
                subject_type="web.path",
                subject_key="web.path:http://10.10.10.10/admin",
                assertion_level="observed",
                payload={"source": "gobuster", "status_code": 403},
                observed_at=now + timedelta(minutes=10),
            ),
            ObservationCreate(
                engagement_id=engagement.id,
                task_id=None,
                source_execution_id="exec-link-upsert-3",
                ingestion_run_id="run-link-upsert-3",
                observation_type="web.path_discovered",
                subject_type="web.path",
                subject_key="web.path:http://10.10.10.10/admin",
                assertion_level="observed",
                payload={"source": "ffuf", "status_code": 200},
                observed_at=now - timedelta(minutes=10),
            ),
        ]
        web_projector.upsert_from_observations(
            db=db,
            user_id=engagement.user_id,
            engagement_id=engagement.id,
            observations=observations,
            asset_key_to_id={},
            service_key_to_id={},
            tenant_id=engagement.tenant_id,
        )
        web_path = db.query(KnowledgeWebPath).filter(KnowledgeWebPath.user_id == engagement.user_id).one()

        link_projector.upsert_web_path_link(
            db=db,
            tenant_id=engagement.tenant_id,
            engagement_id=engagement.id,
            web_path_id=str(web_path.id),
            observed_at=observations[0].observed_at,
        )
        link_projector.upsert_web_path_link(
            db=db,
            tenant_id=engagement.tenant_id,
            engagement_id=engagement.id,
            web_path_id=str(web_path.id),
            observed_at=observations[1].observed_at,
        )
        link_projector.upsert_web_path_link(
            db=db,
            tenant_id=engagement.tenant_id,
            engagement_id=engagement.id,
            web_path_id=str(web_path.id),
            observed_at=observations[2].observed_at,
        )

        links = db.query(EngagementWebPathLink).filter(EngagementWebPathLink.engagement_id == engagement.id).all()
        assert len(links) == 1
        link = links[0]
        assert link.first_seen_in_engagement == observations[2].observed_at.replace(tzinfo=None)
        assert link.last_seen_in_engagement == observations[1].observed_at.replace(tzinfo=None)
    finally:
        db.close()
        engine.dispose()
