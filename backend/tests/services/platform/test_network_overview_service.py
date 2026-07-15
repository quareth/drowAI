"""Unit coverage for topology-aware management and Runner network reporting."""

from __future__ import annotations

import socket
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from fastapi import Request
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from backend.database import Base
from backend.models.runner_control import ExecutionSite, Runner, RunnerConnection
from backend.models.tenant import Tenant
from backend.services.platform.network_overview_service import (
    DefaultGateway,
    HostNetworkDiscovery,
    NetworkOverviewService,
)


def test_host_discovery_prefers_default_route_interface_and_filters_down_links() -> None:
    addresses = {
        "lo0": [SimpleNamespace(family=socket.AF_INET, address="127.0.0.1", netmask="255.0.0.0")],
        "en0": [
            SimpleNamespace(family=socket.AF_INET, address="192.168.50.12", netmask="255.255.255.0"),
            SimpleNamespace(family=socket.AF_INET6, address="fe80::1%en0", netmask="ffff:ffff:ffff:ffff::"),
        ],
        "en9": [SimpleNamespace(family=socket.AF_INET, address="10.0.0.9", netmask="255.255.255.0")],
    }
    stats = {
        "lo0": SimpleNamespace(isup=True),
        "en0": SimpleNamespace(isup=True),
        "en9": SimpleNamespace(isup=False),
    }
    discovery = HostNetworkDiscovery(
        interface_reader=lambda: addresses,
        stats_reader=lambda: stats,
        gateway_reader=lambda: DefaultGateway(ip_address="192.168.50.1", interface_name="en0"),
        dns_reader=lambda: ("1.1.1.1", "2001:4860:4860::8888"),
    )

    snapshot = discovery.collect()

    assert snapshot.primary_ip == "192.168.50.12"
    assert snapshot.gateway == DefaultGateway(ip_address="192.168.50.1", interface_name="en0")
    assert snapshot.dns_servers == ("1.1.1.1", "2001:4860:4860::8888")
    assert [(item.interface_name, item.address, item.family) for item in snapshot.interfaces] == [
        ("en0", "192.168.50.12", "ipv4"),
        ("lo0", "127.0.0.1", "ipv4"),
    ]


def _session() -> Session:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(
        bind=engine,
        tables=[Tenant.__table__, ExecutionSite.__table__, Runner.__table__, RunnerConnection.__table__],
    )
    return sessionmaker(bind=engine, expire_on_commit=False)()


def test_overview_is_tenant_scoped_and_reports_latest_observed_runner_address() -> None:
    db = _session()
    tenant = Tenant(slug="tenant-one", name="Tenant One")
    other_tenant = Tenant(slug="tenant-two", name="Tenant Two")
    db.add_all([tenant, other_tenant])
    db.flush()
    site = ExecutionSite(
        tenant_id=tenant.id,
        name="Istanbul Site",
        slug="istanbul-site",
        network_label="corp-edge",
        status="active",
    )
    other_site = ExecutionSite(
        tenant_id=other_tenant.id,
        name="Other Site",
        slug="other-site",
        status="active",
    )
    db.add_all([site, other_site])
    db.flush()
    runner = Runner(
        tenant_id=tenant.id,
        execution_site_id=site.id,
        name="runner-istanbul",
        status="online",
    )
    other_runner = Runner(
        tenant_id=other_tenant.id,
        execution_site_id=other_site.id,
        name="runner-other",
        status="online",
    )
    db.add_all([runner, other_runner])
    db.flush()
    now = datetime(2026, 7, 10, 12, 0, tzinfo=timezone.utc)
    db.add_all(
        [
            RunnerConnection(
                tenant_id=tenant.id,
                runner_id=runner.id,
                pod_id="pod-a",
                connection_id="older",
                remote_ip_address="198.51.100.10",
                status="disconnected",
                lease_expires_at=now - timedelta(minutes=5),
                last_seen_at=now - timedelta(minutes=10),
            ),
            RunnerConnection(
                tenant_id=tenant.id,
                runner_id=runner.id,
                pod_id="pod-a",
                connection_id="latest",
                remote_ip_address="198.51.100.11",
                status="active",
                lease_expires_at=now + timedelta(minutes=1),
                last_seen_at=now,
            ),
            RunnerConnection(
                tenant_id=other_tenant.id,
                runner_id=other_runner.id,
                pod_id="pod-b",
                connection_id="other",
                remote_ip_address="203.0.113.90",
                status="active",
                lease_expires_at=now + timedelta(minutes=1),
                last_seen_at=now,
            ),
        ]
    )
    db.commit()

    host_snapshot = SimpleNamespace(
        primary_ip="10.20.30.40",
        interfaces=(),
        gateway=DefaultGateway(ip_address="10.20.30.1", interface_name="eth0"),
        dns_servers=("10.20.30.53",),
    )
    management_urls = SimpleNamespace(
        resolve=lambda request: SimpleNamespace(
            management_url="https://management.example.test",
            source="generated_config",
        )
    )
    service = NetworkOverviewService(
        db,
        host_discovery=SimpleNamespace(collect=lambda: host_snapshot),
        management_url_service=management_urls,
        deployment_profile_resolver=lambda: SimpleNamespace(profile=SimpleNamespace(value="distributed")),
        now_provider=lambda: now,
    )
    request = Request({"type": "http", "scheme": "https", "server": ("example.test", 443), "headers": []})

    response = service.collect(tenant_id=tenant.id, request=request)

    assert response.deployment_profile == "distributed"
    assert response.management.advertised_url == "https://management.example.test"
    assert response.management.advertised_host == "management.example.test"
    assert response.management.primary_ip == "10.20.30.40"
    assert len(response.runners) == 1
    assert response.runners[0].name == "runner-istanbul"
    assert response.runners[0].site_name == "Istanbul Site"
    assert response.runners[0].observed_ip == "198.51.100.11"
    assert response.runners[0].connection_status == "connected"
