"""Tests for service/asset-scoped web-surface selectors and query methods."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import uuid as uuid_lib

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.database import Base
from backend.models.core import Engagement, User
from backend.models.knowledge import (
    EngagementAssetLink,
    EngagementServiceLink,
    EngagementWebPathLink,
    KnowledgeAsset,
    KnowledgeService,
    KnowledgeWebPath,
)
from backend.services.knowledge.query.contracts import WebSurfacePathsFilters
from backend.services.knowledge.query_service import KnowledgeQueryService


def _build_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    return engine, session_factory()


def _stable_uuid(token: str) -> uuid_lib.UUID:
    return uuid_lib.uuid5(uuid_lib.NAMESPACE_DNS, f"drowai-web-surface-query-{token}")


def _seed_user(db, username: str) -> User:
    user = User(username=username, password="secret")
    db.add(user)
    db.flush()
    return user


def _seed_web_surface_sample(db):
    now = datetime(2026, 4, 22, 10, 0, tzinfo=timezone.utc)
    tenant_id = 501
    foreign_tenant_id = 502
    owner = _seed_user(db, "web-surface-owner")
    foreign = _seed_user(db, "web-surface-foreign")

    engagement = Engagement(
        user_id=owner.id,
        tenant_id=tenant_id,
        name="Web Surface",
        status="active",
        created_at=now,
        updated_at=now,
    )
    other_engagement = Engagement(
        user_id=owner.id,
        tenant_id=tenant_id,
        name="Other Engagement",
        status="active",
        created_at=now,
        updated_at=now,
    )
    db.add_all([engagement, other_engagement])
    db.flush()

    service = KnowledgeService(
        id=_stable_uuid("service"),
        tenant_id=tenant_id,
        user_id=owner.id,
        engagement_id=engagement.id,
        service_key="service.socket:10.10.10.10/tcp/443",
        protocol="tcp",
        port=443,
        service_name="https",
        status="open",
        first_seen_at=now - timedelta(hours=2),
        last_seen_at=now,
        service_metadata={},
    )
    db.add(service)
    db.flush()

    db.add(
        EngagementServiceLink(
            tenant_id=tenant_id,
            engagement_id=engagement.id,
            service_id=service.id,
            first_seen_in_engagement=now - timedelta(hours=2),
            last_seen_in_engagement=now,
        )
    )

    web_paths = [
        KnowledgeWebPath(
            id=_stable_uuid("wp-admin"),
            tenant_id=tenant_id,
            user_id=owner.id,
            service_id=service.id,
            canonical_url="https://example.com/admin",
            origin_key="https://example.com",
            path="/admin",
            last_status_code=200,
            last_response_size=4321,
            calibrated_baseline=False,
            noise_score=0.0,
            first_seen_at=now - timedelta(hours=4),
            last_seen_at=now - timedelta(minutes=15),
            producer_summary={"web_applications.web_application_fuzzers.ffuf": {"seen_count": 1}},
            evidence_refs=[],
        ),
        KnowledgeWebPath(
            id=_stable_uuid("wp-health"),
            tenant_id=tenant_id,
            user_id=owner.id,
            service_id=service.id,
            canonical_url="https://example.com/health",
            origin_key="https://example.com",
            path="/health",
            last_status_code=200,
            last_response_size=200,
            calibrated_baseline=True,
            noise_score=0.5,
            first_seen_at=now - timedelta(hours=3),
            last_seen_at=now - timedelta(minutes=5),
            producer_summary={"web_applications.web_crawlers.gobuster": {"seen_count": 2}},
            evidence_refs=[],
        ),
        KnowledgeWebPath(
            id=_stable_uuid("wp-login"),
            tenant_id=tenant_id,
            user_id=owner.id,
            service_id=service.id,
            canonical_url="https://example.com/login",
            origin_key="https://example.com",
            path="/login",
            last_status_code=401,
            last_response_size=1280,
            calibrated_baseline=False,
            noise_score=0.2,
            first_seen_at=now - timedelta(hours=2),
            last_seen_at=now - timedelta(minutes=10),
            producer_summary={"web_applications.web_application_fuzzers.ffuf": {"seen_count": 3}},
            evidence_refs=[],
        ),
        KnowledgeWebPath(
            id=_stable_uuid("wp-api"),
            tenant_id=tenant_id,
            user_id=owner.id,
            service_id=service.id,
            canonical_url="https://api.example.com/v1",
            origin_key="https://api.example.com",
            path="/v1",
            last_status_code=200,
            last_response_size=1500,
            calibrated_baseline=False,
            noise_score=0.0,
            first_seen_at=now - timedelta(hours=5),
            last_seen_at=now - timedelta(minutes=1),
            producer_summary={"web_applications.web_application_fuzzers.ffuf": {"seen_count": 1}},
            evidence_refs=[],
        ),
    ]
    db.add_all(web_paths)
    db.flush()

    for row in web_paths:
        db.add(
            EngagementWebPathLink(
                tenant_id=tenant_id,
                engagement_id=engagement.id,
                web_path_id=row.id,
                first_seen_in_engagement=row.first_seen_at,
                last_seen_in_engagement=row.last_seen_at,
            )
        )

    db.commit()
    return {
        "owner": owner,
        "foreign": foreign,
        "tenant_id": tenant_id,
        "foreign_tenant_id": foreign_tenant_id,
        "engagement": engagement,
        "other_engagement": other_engagement,
        "service": service,
    }


def test_service_bound_origin_summary_is_keyed_by_service_key() -> None:
    engine, db = _build_session()
    try:
        seeded = _seed_web_surface_sample(db)
        service = KnowledgeQueryService(db)

        payload = service.list_web_surface_origins(
            user_id=seeded["owner"].id,
            tenant_id=seeded["tenant_id"],
            engagement_id=seeded["engagement"].id,
            service_key=seeded["service"].service_key,
            include_noisy=False,
        )

        assert payload["service_key"] == seeded["service"].service_key
        items = payload["items"]
        assert len(items) == 2
        example_origin = next(item for item in items if item["origin_key"] == "https://example.com")
        assert example_origin["total_paths"] == 3
        assert example_origin["visible_paths"] == 2
        assert example_origin["hidden_noisy"] == 1
        assert example_origin["calibrated_warnings"] == 1
        assert set(example_origin["producers"]) == {
            "web_applications.web_application_fuzzers.ffuf",
            "web_applications.web_crawlers.gobuster",
        }
    finally:
        db.close()
        engine.dispose()


def test_service_bound_path_page_hides_noisy_rows_by_default() -> None:
    engine, db = _build_session()
    try:
        seeded = _seed_web_surface_sample(db)
        service = KnowledgeQueryService(db)

        payload = service.list_web_surface_paths(
            user_id=seeded["owner"].id,
            tenant_id=seeded["tenant_id"],
            engagement_id=seeded["engagement"].id,
            filters=WebSurfacePathsFilters(
                service_key=seeded["service"].service_key,
                origin_key=" https://Example.com:443/dashboard?x=1 ",
                include_noisy=False,
                limit=50,
                offset=0,
            ),
        )

        assert payload["service_key"] == seeded["service"].service_key
        assert payload["total"] == 2
        assert payload["hidden_noisy"] == 1
        assert [item["canonical_url"] for item in payload["items"]] == [
            "https://example.com/admin",
            "https://example.com/login",
        ]
    finally:
        db.close()
        engine.dispose()


def test_service_bound_path_page_can_include_noisy_rows() -> None:
    engine, db = _build_session()
    try:
        seeded = _seed_web_surface_sample(db)
        service = KnowledgeQueryService(db)

        payload = service.list_web_surface_paths(
            user_id=seeded["owner"].id,
            tenant_id=seeded["tenant_id"],
            engagement_id=seeded["engagement"].id,
            filters=WebSurfacePathsFilters(
                service_key=seeded["service"].service_key,
                origin_key="https://example.com",
                include_noisy=True,
                limit=50,
                offset=0,
            ),
        )

        assert payload["total"] == 3
        assert payload["hidden_noisy"] == 0
        assert [item["canonical_url"] for item in payload["items"]] == [
            "https://example.com/admin",
            "https://example.com/login",
            "https://example.com/health",
        ]
    finally:
        db.close()
        engine.dispose()


def test_service_scope_uses_asset_only_for_unassociated_path_fallback() -> None:
    engine, db = _build_session()
    try:
        seeded = _seed_web_surface_sample(db)
        now = datetime(2026, 4, 22, 11, 0, tzinfo=timezone.utc)
        asset = KnowledgeAsset(
            id=_stable_uuid("asset"),
            tenant_id=seeded["tenant_id"],
            user_id=seeded["owner"].id,
            engagement_id=seeded["engagement"].id,
            asset_key="host.ip:10.10.10.10",
            asset_type="host.ip",
            display_name="10.10.10.10",
            ip_address="10.10.10.10",
            first_seen_at=now,
            last_seen_at=now,
            asset_metadata={},
        )
        db.add(asset)
        db.flush()
        seeded["service"].asset_id = asset.id
        db.add(
            EngagementAssetLink(
                tenant_id=seeded["tenant_id"],
                engagement_id=seeded["engagement"].id,
                asset_id=asset.id,
                first_seen_in_engagement=now,
                last_seen_in_engagement=now,
            )
        )

        other_service = KnowledgeService(
            id=_stable_uuid("other-service"),
            tenant_id=seeded["tenant_id"],
            user_id=seeded["owner"].id,
            engagement_id=seeded["engagement"].id,
            service_key="service.socket:10.10.10.10/tcp/8443",
            asset_id=asset.id,
            protocol="tcp",
            port=8443,
            service_name="https",
            first_seen_at=now,
            last_seen_at=now,
            service_metadata={},
        )
        fallback_path = KnowledgeWebPath(
            id=_stable_uuid("fallback-path"),
            tenant_id=seeded["tenant_id"],
            user_id=seeded["owner"].id,
            asset_id=asset.id,
            service_id=None,
            canonical_url="https://fallback.example.com/admin",
            origin_key="https://fallback.example.com",
            path="/admin",
            noise_score=0.0,
            first_seen_at=now,
            last_seen_at=now,
            producer_summary={"ffuf": {"seen_count": 1}},
            evidence_refs=[],
        )
        other_service_path = KnowledgeWebPath(
            id=_stable_uuid("other-service-path"),
            tenant_id=seeded["tenant_id"],
            user_id=seeded["owner"].id,
            asset_id=asset.id,
            service_id=other_service.id,
            canonical_url="https://other.example.com/private",
            origin_key="https://other.example.com",
            path="/private",
            noise_score=0.0,
            first_seen_at=now,
            last_seen_at=now,
            producer_summary={"ffuf": {"seen_count": 1}},
            evidence_refs=[],
        )
        db.add_all([other_service, fallback_path, other_service_path])
        db.flush()
        for web_path in (fallback_path, other_service_path):
            db.add(
                EngagementWebPathLink(
                    tenant_id=seeded["tenant_id"],
                    engagement_id=seeded["engagement"].id,
                    web_path_id=web_path.id,
                    first_seen_in_engagement=now,
                    last_seen_in_engagement=now,
                )
            )
        db.commit()

        payload = KnowledgeQueryService(db).list_web_surface_paths(
            user_id=seeded["owner"].id,
            tenant_id=seeded["tenant_id"],
            engagement_id=seeded["engagement"].id,
            filters=WebSurfacePathsFilters(
                service_key=seeded["service"].service_key,
                asset_key=asset.asset_key,
                include_noisy=True,
            ),
        )

        urls = {item["canonical_url"] for item in payload["items"]}
        assert fallback_path.canonical_url in urls
        assert other_service_path.canonical_url not in urls
    finally:
        db.close()
        engine.dispose()


def test_service_engagement_ownership_is_enforced_in_selectors() -> None:
    engine, db = _build_session()
    try:
        seeded = _seed_web_surface_sample(db)
        service = KnowledgeQueryService(db)

        wrong_engagement = service.list_web_surface_paths(
            user_id=seeded["owner"].id,
            tenant_id=seeded["tenant_id"],
            engagement_id=seeded["other_engagement"].id,
            filters=WebSurfacePathsFilters(
                service_key=seeded["service"].service_key,
                origin_key="https://example.com",
            ),
        )
        foreign_owner = service.list_web_surface_paths(
            user_id=seeded["foreign"].id,
            tenant_id=seeded["foreign_tenant_id"],
            engagement_id=seeded["engagement"].id,
            filters=WebSurfacePathsFilters(
                service_key=seeded["service"].service_key,
                origin_key="https://example.com",
            ),
        )

        assert wrong_engagement["items"] == []
        assert wrong_engagement["total"] == 0
        assert foreign_owner["items"] == []
        assert foreign_owner["total"] == 0
    finally:
        db.close()
        engine.dispose()
