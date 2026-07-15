"""Pydantic request/response schemas for VPN task configuration.

Scope:
- Defines VPN configuration payloads and VPN-aware task schema variants.
- Extends task creation/response contracts for VPN-enabled workflows.

Boundaries:
- API schema definitions only; no VPN connection orchestration or persistence logic.
- Imports base task schemas from `backend.schemas.core`.
"""

from datetime import datetime
from typing import Optional, Literal

from pydantic import BaseModel, ConfigDict, field_validator

from backend.schemas.core import TaskCreate, TaskResponse


class VPNConfigBase(BaseModel):
    """Base VPN configuration model."""

    provider: Literal["htb", "tryhackme", "custom"]
    config_data: str  # Base64 encoded OVPN file or manual config

    @field_validator("config_data")
    @classmethod
    def validate_config_data(cls, v: str) -> str:
        if not v or len(v.strip()) == 0:
            raise ValueError("VPN configuration data is required")
        return v.strip()


class VPNConfigCreate(VPNConfigBase):
    """VPN configuration for task creation."""


class VPNStatusResponse(BaseModel):
    """VPN status response."""

    connection_status: str
    ip_address: Optional[str] = None
    connected_at: Optional[datetime] = None
    error_message: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)


class TaskCreateVPN(TaskCreate):
    """Extended task creation with VPN support."""

    vpn_enabled: bool = False
    vpn_config: Optional[VPNConfigCreate] = None


class TaskResponseVPN(TaskResponse):
    """Extended task response with VPN status."""

    vpn_enabled: bool
    vpn_provider: Optional[str] = None
    vpn_connection_status: str
    vpn_ip_address: Optional[str] = None
    vpn_connected_at: Optional[datetime] = None
