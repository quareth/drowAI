"""Alembic environment configuration for the DrowAI backend schema.

This module loads deployment configuration, imports every ORM model so Alembic
sees the complete metadata graph, and runs migrations under the privileged RLS
maintenance context required for PostgreSQL tenant-isolation objects.
"""

import os
import sys
from logging.config import fileConfig

# Load .env from project root (parent of backend) so DATABASE_URL is set when running alembic
_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if sys.path[0:1] != [_root]:
    sys.path.insert(0, _root)
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(_root, ".env"))
except ImportError:
    pass

from sqlalchemy import engine_from_config, pool  # noqa: E402
from alembic import context  # noqa: E402

from backend.database import Base  # noqa: E402
import backend.models  # noqa: F401,E402
from backend.services.tenant.rls import privileged_rls_bypass  # noqa: E402

# This is the Alembic Config object
config = context.config

# Interpret the config file for Python logging
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def get_database_url() -> str:
    """Get database URL from environment or config.
    
    Priority:
    1. DATABASE_URL environment variable
    2. sqlalchemy.url from alembic.ini (if set)
    
    Raises:
        RuntimeError: If no database URL is configured
    """
    # Try environment variable first
    url = os.environ.get("DATABASE_URL")
    if url:
        # Normalize postgres:// to postgresql:// for SQLAlchemy 2.x
        if url.startswith("postgres://"):
            url = url.replace("postgres://", "postgresql://", 1)
        return url
    
    # Fall back to config file
    url = config.get_main_option("sqlalchemy.url")
    if url:
        return url
    
    raise RuntimeError(
        "DATABASE_URL environment variable not set. "
        "Export it before running migrations:\n"
        "  export DATABASE_URL=postgresql+psycopg2://user:pass@localhost:5432/drowai"
    )


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    This configures the context with just a URL and not an Engine,
    though an Engine is acceptable here as well. By skipping the Engine
    creation we don't even need a DBAPI to be available.

    Calls to context.execute() here emit the given string to the
    script output.
    """
    url = get_database_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode.

    In this scenario we need to create an Engine and associate a
    connection with the context.
    """
    # Override sqlalchemy.url with environment variable
    configuration = config.get_section(config.config_ini_section) or {}
    configuration["sqlalchemy.url"] = get_database_url()

    connectable = engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.begin() as connection:
        # Migrations run under an explicit privileged RLS maintenance context.
        with privileged_rls_bypass(connection, scope="migration", actor_type="system"):
            context.configure(
                connection=connection,
                target_metadata=target_metadata,
            )

            with context.begin_transaction():
                context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
