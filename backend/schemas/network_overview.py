"""Read-only API contracts for deployment-aware network observability."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field


class NetworkInterfaceAddress(BaseModel):
    """One active Management interface address."""

    interface_name: str = Field(min_length=1)
    address: str = Field(min_length=1)
    family: str = Field(pattern="^ipv[46]$")
    prefix_length: int | None = Field(default=None, ge=0, le=128)
    is_loopback: bool


class ManagementNetworkOverview(BaseModel):
    """Advertised and locally observed Management network state."""

    advertised_url: str | None = None
    advertised_host: str | None = None
    advertised_url_source: str
    primary_ip: str | None = None
    interfaces: list[NetworkInterfaceAddress]
    gateway_ip: str | None = None
    gateway_interface: str | None = None
    dns_servers: list[str]


class RunnerNetworkOverview(BaseModel):
    """Tenant Runner connectivity address observed by Management."""

    id: UUID
    name: str
    site_id: UUID
    site_name: str
    site_network_label: str | None = None
    status: str
    connection_status: str
    observed_ip: str | None = None
    observed_at: datetime | None = None


class NetworkOverviewResponse(BaseModel):
    """Deployment topology and current Management/Runner network projection."""

    deployment_profile: str
    management: ManagementNetworkOverview
    runners: list[RunnerNetworkOverview]
    collected_at: datetime
