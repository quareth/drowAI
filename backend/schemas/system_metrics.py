"""Response contracts for the authenticated system resource metrics API."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class ResourceUsage(BaseModel):
    """Capacity and utilization values for one byte-addressable resource."""

    total_bytes: int = Field(ge=0)
    used_bytes: int = Field(ge=0)
    available_bytes: int = Field(ge=0)
    usage_percent: float = Field(ge=0, le=100)


class SystemMetricsResponse(BaseModel):
    """Point-in-time management host metrics shown in system settings."""

    memory: ResourceUsage
    storage: ResourceUsage
    uptime_seconds: int = Field(ge=0)
    collected_at: datetime
