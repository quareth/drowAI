from __future__ import annotations

from fastapi import APIRouter

from backend.services.metrics import metrics
from backend.services.streaming.db_stream_service import get_db_stream_service

router = APIRouter()


@router.get("/health/streaming")
async def streaming_health():
    """Basic health for DB streaming & metrics."""
    svc = get_db_stream_service()
    svc_metrics = svc.get_metrics()
    return {
        "ok": True,
        "db_stream": svc_metrics,
        "metrics": metrics.snapshot(),
    }


