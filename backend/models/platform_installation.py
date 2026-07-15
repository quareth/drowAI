"""Platform installation singleton ORM model for standalone setup wizard state.

Stores first-run wizard progress, display defaults, and placeholder networking
configuration in PostgreSQL instead of relying on `.env` presence alone.
"""

from __future__ import annotations

from sqlalchemy import Column, DateTime, Integer, JSON, String, Text
from sqlalchemy.sql import func

from backend.database import Base

PLATFORM_INSTALLATION_SINGLETON_ID = 1


class PlatformInstallation(Base):
    """Singleton row tracking standalone platform installation lifecycle."""

    __tablename__ = "platform_installations"

    id = Column(Integer, primary_key=True, default=PLATFORM_INSTALLATION_SINGLETON_ID)
    completed_at = Column(DateTime(timezone=True), nullable=True)
    status = Column(String(32), nullable=False, default="pending", server_default="pending", index=True)
    setup_error = Column(Text, nullable=True)
    provisioning_metadata = Column(JSON, nullable=True)
    deployment_profile = Column(String(32), nullable=False, default="dev_local")
    network_config = Column(JSON, nullable=True)
    display_defaults = Column(JSON, nullable=True)
    setup_version = Column(Integer, nullable=False, default=1, server_default="1")
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
