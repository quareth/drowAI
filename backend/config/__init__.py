"""
Configuration module for DrowAI backend
"""
import os
import re
import logging
import time
from pathlib import Path

from core.llm import LLM_TIMEOUT_INTENT_CLASSIFIER_SEC
from backend.config.generated_config import resolved_backend_env

try:
    from dotenv import load_dotenv

    # Make config import-order safe: ensure .env is loaded even when modules
    # import backend.config directly (without going through backend.main).
    _REPO_ROOT = Path(__file__).resolve().parents[2]
    _ENV_FILE = _REPO_ROOT / ".env"
    if _ENV_FILE.exists():
        load_dotenv(dotenv_path=_ENV_FILE)
    else:
        load_dotenv()
except Exception:
    # dotenv missing or unreadable environment file; continue with OS env.
    pass

try:
    for _key, _value in resolved_backend_env(
        profile=os.getenv("DROWAI_DEPLOYMENT_PROFILE", "dev_local"),
        docker=bool(os.getenv("DROWAI_CONFIG_DIR") or os.getenv("DROWAI_SECRETS_DIR")),
    ).items():
        os.environ.setdefault(_key, _value)
except Exception:
    # Generated config is best-effort here; dedicated startup paths surface
    # bootstrap failures with clearer process-level errors.
    pass


def _read_int_env(key: str, default: int) -> int:
    """Return int env value, falling back to default on invalid input."""
    raw = os.getenv(key)
    if raw is None:
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def _read_positive_int_env(key: str, default: int) -> int:
    """Return a positive int env value or default."""
    value = _read_int_env(key, default)
    return value if value > 0 else default


def _read_size_env(key: str, default: str) -> str:
    """Return Docker size env value (e.g. 512m, 2g) or default."""
    raw = os.getenv(key)
    if raw is None:
        return default

    value = raw.strip().lower()
    if re.fullmatch(r"\d+[bkmg]?", value):
        return value
    return default


def _read_csv_list_env(key: str, default: tuple[str, ...]) -> tuple[str, ...]:
    """Return normalized CSV env list, or default when empty/invalid."""
    raw = os.getenv(key)
    if raw is None:
        return default
    values = tuple(item.strip().lower() for item in raw.split(",") if item.strip())
    return values or default


def _parse_allowed_origins(raw: str | None) -> tuple[str, ...]:
    """Return explicit CORS origins suitable for credentialed browser requests."""
    default = ("http://localhost:5000", "http://127.0.0.1:5000")
    if raw is None:
        return default
    origins = tuple(
        origin.strip().rstrip("/")
        for origin in raw.split(",")
        if origin.strip()
    )
    if not origins:
        return default
    if "*" in origins:
        raise ValueError(
            "ALLOWED_ORIGINS must list explicit origins because DrowAI uses credentialed requests."
        )
    return origins


def _read_rollout_stage_env(
    key: str, default: str = "off", allowed: tuple[str, ...] = ("off", "internal", "beta", "ga")
) -> str:
    """Return normalized rollout stage or default when unset/invalid."""
    raw = os.getenv(key)
    if raw is None:
        return default
    stage = raw.strip().lower()
    return stage if stage in allowed else default


def _read_percent_env(key: str, default: int) -> int:
    """Return rollout percentage clamped to 0..100."""
    value = _read_int_env(key, default)
    if value < 0:
        return 0
    if value > 100:
        return 100
    return value


def _read_bool_env(key: str, default: bool = False) -> bool:
    """Return normalized bool env value with permissive truthy parsing."""
    raw = os.getenv(key)
    if raw is None:
        return default
    normalized = str(raw).strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return default


# Feature flags
ENABLE_PERSISTENT_TERMINALS = _read_bool_env("ENABLE_PERSISTENT_TERMINALS", False)
# LangGraph branch availability (always on; not environment-configurable).
ENABLE_LANGGRAPH_DEEP_REASONING = True
ENABLE_LANGGRAPH_SIMPLE_TOOL = True
ENABLE_TURN_BASED_PERSISTENCE = True
ENABLE_LANGGRAPH_FORCE_SIMPLE_CHAT = _read_bool_env("ENABLE_LANGGRAPH_FORCE_SIMPLE_CHAT", False)
# Deprecated alias; use core.llm.timeouts.LLM_TIMEOUT_INTENT_CLASSIFIER_SEC.
LANGGRAPH_INTENT_CLASSIFIER_TIMEOUT_SEC = LLM_TIMEOUT_INTENT_CLASSIFIER_SEC
ENABLE_HITL_INTERRUPTS = _read_bool_env("ENABLE_HITL_INTERRUPTS", True)
ENABLE_CONTEXT_COMPRESSION = _read_bool_env("ENABLE_CONTEXT_COMPRESSION", True)
E2E_DETERMINISTIC_MODE = _read_bool_env("E2E_DETERMINISTIC_MODE", False)
# Real local-Docker browser certification: keeps suite data/test scope isolated
# while deliberately leaving deterministic lifecycle shortcuts disabled.
E2E_RUNTIME_LOCAL_MODE = _read_bool_env("E2E_RUNTIME_LOCAL_MODE", False)

# Terminal session configuration
TERMINAL_SESSION_TIMEOUT = int(os.getenv('TERMINAL_SESSION_TIMEOUT', '3600'))  # 1 hour
TERMINAL_CLEANUP_INTERVAL = int(os.getenv('TERMINAL_CLEANUP_INTERVAL', '300'))  # 5 minutes
MAX_SESSIONS_PER_USER = int(os.getenv('MAX_SESSIONS_PER_USER', '10'))
MAX_TOTAL_SESSIONS = int(os.getenv('MAX_TOTAL_SESSIONS', '1000'))

# Output buffer configuration
MAX_BUFFER_SIZE = int(os.getenv('MAX_BUFFER_SIZE', '10000'))  # lines
MAX_BUFFER_MEMORY = int(os.getenv('MAX_BUFFER_MEMORY', '52428800'))  # 50MB

# Docker configuration
DOCKER_SOCKET = os.getenv('DOCKER_SOCKET', '/var/run/docker.sock')
DOCKER_TIMEOUT = int(os.getenv('DOCKER_TIMEOUT', '30'))

# WebSocket configuration
WEBSOCKET_PING_INTERVAL = int(os.getenv('WEBSOCKET_PING_INTERVAL', '30'))
WEBSOCKET_PING_TIMEOUT = int(os.getenv('WEBSOCKET_PING_TIMEOUT', '10'))

# Logging configuration
LOG_LEVEL = os.getenv('LOG_LEVEL', 'INFO')
LOG_FORMAT = os.getenv('LOG_FORMAT', '%(asctime)s - %(name)s - %(levelname)s - %(message)s')

# Force all Python logging formatter timestamps to UTC.
logging.Formatter.converter = time.gmtime

# Database configuration. Product and local management-plane paths use PostgreSQL;
# tests may still provide an explicit SQLite URL.
DATABASE_URL = os.getenv("DATABASE_URL", "")

# Auth token lifetime (signing secret is resolved in backend.auth from JWT_SECRET).
ACCESS_TOKEN_EXPIRE_MINUTES = int(os.getenv('ACCESS_TOKEN_EXPIRE_MINUTES', '30'))

# CORS configuration
ALLOWED_ORIGINS = _parse_allowed_origins(os.getenv("ALLOWED_ORIGINS"))

# Development configuration
DEBUG = _read_bool_env("DEBUG", False)
RELOAD = _read_bool_env("RELOAD", False) 

# Reasoning/History feature flags (Phase 0)
# - REASONING_DB_PERSIST: enable DB persistence (dual-write) of reasoning steps
# - REASONING_DB_STREAM: enable DB-backed history endpoint (no longer controls live streaming;
#                        all live streaming now goes through InMemoryStreamHub regardless of this flag)
REASONING_DB_PERSIST = _read_bool_env("REASONING_DB_PERSIST", True if DEBUG else True)
REASONING_DB_STREAM = _read_bool_env("REASONING_DB_STREAM", False if DEBUG else True)
# Multiplexed WebSocket subscriptions for reasoning (always on; primary live stream path).
REASONING_WS_MULTIPLEX = True
REASONING_WS_MAX_SUBSCRIPTIONS = int(os.getenv('REASONING_WS_MAX_SUBSCRIPTIONS', '3'))

# DB Streaming Configuration (Phase 1)
DB_STREAM_POLL_INTERVAL_MS = int(os.getenv('DB_STREAM_POLL_INTERVAL_MS', '250'))
DB_STREAM_REPLAY_BATCH_SIZE = int(os.getenv('DB_STREAM_REPLAY_BATCH_SIZE', '500'))
DB_STREAM_MAX_CONNECTIONS_PER_TASK = int(os.getenv('DB_STREAM_MAX_CONNECTIONS_PER_TASK', '10'))
DB_STREAM_HEARTBEAT_INTERVAL_SEC = int(os.getenv('DB_STREAM_HEARTBEAT_INTERVAL_SEC', '30'))

# Streaming/backpressure defaults (tunable; placeholders for later phases)
REASONING_SSE_MAX_QUEUE = int(os.getenv('REASONING_SSE_MAX_QUEUE', '2000'))
REASONING_SSE_IDLE_TIMEOUT_SEC = int(os.getenv('REASONING_SSE_IDLE_TIMEOUT_SEC', '0'))  # 0 = disabled
 
# Observability & retention (Phase 4)
METRICS_ENABLED = _read_bool_env("METRICS_ENABLED", True)
METRICS_LOG_INTERVAL_SEC = int(os.getenv('METRICS_LOG_INTERVAL_SEC', '60'))
REASONING_RETENTION_DAYS = int(os.getenv('REASONING_RETENTION_DAYS', '30'))

# Agent Mock Mode (Development Feature)
# Set AGENT_REASONING_MOCK_MODE=true to disable real AI reasoning and use mock data
# This prevents API token consumption during development and testing
AGENT_REASONING_MOCK_MODE = _read_bool_env("AGENT_REASONING_MOCK_MODE", False)

# Chat-style SSE (token delta) pacing configuration
# Deltas are always enabled for real agents; this only tunes pacing
REASONING_SSE_CHAT_CHUNK_MS = int(os.getenv('REASONING_SSE_CHAT_CHUNK_MS', '25'))  # per char

__all__ = [
    "ACCESS_TOKEN_EXPIRE_MINUTES",
    "AGENT_REASONING_MOCK_MODE",
    "ALLOWED_ORIGINS",
    "DATABASE_URL",
    "DB_STREAM_HEARTBEAT_INTERVAL_SEC",
    "DB_STREAM_MAX_CONNECTIONS_PER_TASK",
    "DB_STREAM_POLL_INTERVAL_MS",
    "DB_STREAM_REPLAY_BATCH_SIZE",
    "DEBUG",
    "DOCKER_SOCKET",
    "DOCKER_TIMEOUT",
    "E2E_DETERMINISTIC_MODE",
    "E2E_RUNTIME_LOCAL_MODE",
    "ENABLE_HITL_INTERRUPTS",
    "ENABLE_CONTEXT_COMPRESSION",
    "ENABLE_LANGGRAPH_DEEP_REASONING",
    "ENABLE_LANGGRAPH_FORCE_SIMPLE_CHAT",
    "ENABLE_LANGGRAPH_SIMPLE_TOOL",
    "ENABLE_PERSISTENT_TERMINALS",
    "ENABLE_TURN_BASED_PERSISTENCE",
    "LANGGRAPH_INTENT_CLASSIFIER_TIMEOUT_SEC",
    "LOG_FORMAT",
    "LOG_LEVEL",
    "MAX_BUFFER_MEMORY",
    "MAX_BUFFER_SIZE",
    "MAX_SESSIONS_PER_USER",
    "MAX_TOTAL_SESSIONS",
    "METRICS_ENABLED",
    "METRICS_LOG_INTERVAL_SEC",
    "REASONING_DB_PERSIST",
    "REASONING_DB_STREAM",
    "REASONING_RETENTION_DAYS",
    "REASONING_SSE_CHAT_CHUNK_MS",
    "REASONING_SSE_IDLE_TIMEOUT_SEC",
    "REASONING_SSE_MAX_QUEUE",
    "REASONING_WS_MAX_SUBSCRIPTIONS",
    "REASONING_WS_MULTIPLEX",
    "RELOAD",
    "TERMINAL_CLEANUP_INTERVAL",
    "TERMINAL_SESSION_TIMEOUT",
    "WEBSOCKET_PING_INTERVAL",
    "WEBSOCKET_PING_TIMEOUT",
]
