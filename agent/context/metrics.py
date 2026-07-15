
"""Context monitoring and metrics collection utilities."""

from __future__ import annotations

import os
import time

from collections import defaultdict
from typing import Any, Dict, List, DefaultDict, Optional

try:
    from agent.logger import AgentLogger
except Exception:  # pragma: no cover - fallback for tests
    from logger import AgentLogger

try:
    import psutil  # type: ignore
    PSUTIL_AVAILABLE = True
except Exception:  # pragma: no cover - psutil optional
    PSUTIL_AVAILABLE = False

try:
    import resource
    RESOURCE_AVAILABLE = True
except ImportError:
    RESOURCE_AVAILABLE = False


class ContextMetrics:
    """Collect and export runtime metrics for context management."""

    def __init__(self, logger: Optional[AgentLogger] = None) -> None:
        self.logger = logger
        self.token_usage: DefaultDict[str, List[Dict[str, Any]]] = defaultdict(list)
        self.compression_ratios: DefaultDict[str, List[float]] = defaultdict(list)
        self.processing_times: DefaultDict[str, List[int]] = defaultdict(list)
        self.quality_scores: List[float] = []
        self.resource_usage: List[Dict[str, float]] = []
        # Artifact-specific metrics
        self.artifact_ingestions: List[Dict[str, Any]] = []
        self.artifact_queries: List[Dict[str, Any]] = []

    # ------------------------------------------------------------------
    # Recording methods
    # ------------------------------------------------------------------
    def record_token_usage(self, component: str, tokens: int, budget: int) -> None:
        """Record token usage for a component and alert when near budget."""
        entry = {
            "timestamp": time.time(),
            "tokens": int(tokens),
            "budget": int(budget),
        }
        self.token_usage[component].append(entry)
        if self.logger and budget > 0 and tokens >= budget * 0.9:
            self.logger.warning(
                f"Token usage for {component} approaching limit: {tokens}/{budget}"
            )

    def record_compression(self, content_type: str, original_size: int, compressed_size: int) -> None:
        """Record compression ratio for a content type."""
        ratio = compressed_size / float(original_size or 1)
        self.compression_ratios[content_type].append(ratio)

    def record_processing_time(self, operation: str, duration_ms: int) -> None:
        """Record processing time for an operation and warn on slowness."""
        self.processing_times[operation].append(int(duration_ms))
        if self.logger and duration_ms > 1000:
            self.logger.warning(
                f"Performance degradation detected for {operation}: {duration_ms}ms"
            )

    # Artifact metrics -------------------------------------------------
    def record_artifact_ingestion(
        self,
        *,
        run_id: str,
        tool: str,
        artifact_path: str,
        ingestion_time_ms: int,
        chunks_count: int,
        avg_chunk_tokens: int,
    ) -> None:
        entry = {
            "timestamp": time.time(),
            "run_id": run_id,
            "tool": tool,
            "artifact_path": artifact_path,
            "ingestion_time_ms": ingestion_time_ms,
            "chunks_count": chunks_count,
            "avg_chunk_tokens": avg_chunk_tokens,
        }
        self.artifact_ingestions.append(entry)

    def record_artifact_query(
        self,
        *,
        query_latency_ms: int,
        candidate_count: int,
        selected_count: int,
        pack_tokens: int,
        topk: int,
        overflow: bool,
    ) -> None:
        entry = {
            "timestamp": time.time(),
            "query_latency_ms": query_latency_ms,
            "candidate_count": candidate_count,
            "selected_count": selected_count,
            "pack_tokens": pack_tokens,
            "topk": topk,
            "overflow": overflow,
        }
        self.artifact_queries.append(entry)

    def record_quality_score(self, score: float) -> None:
        """Record quality score from compression or reasoning."""
        self.quality_scores.append(float(score))

    def record_resource_usage(self) -> None:
        """Capture current memory and CPU usage."""
        mem = self._memory_mb()
        cpu = self._cpu_percent()
        self.resource_usage.append(
            {
                "timestamp": time.time(),
                "memory_mb": mem,
                "cpu_percent": cpu,
            }
        )
        if self.logger and mem > 500:
            self.logger.warning(f"High memory usage detected: {mem:.2f}MB")

    # ------------------------------------------------------------------
    # Export
    # ------------------------------------------------------------------
    def export_metrics(self) -> Dict[str, Any]:
        """Return summarized metrics for reporting."""
        total_used = 0
        total_budget = 0
        for entries in self.token_usage.values():
            for e in entries:
                total_used += e["tokens"]
                total_budget += e["budget"]
        token_savings = max(total_budget - total_used, 0)

        avg_processing = {
            op: (sum(vals) / len(vals)) for op, vals in self.processing_times.items() if vals
        }
        avg_quality = (
            sum(self.quality_scores) / len(self.quality_scores)
            if self.quality_scores
            else 0.0
        )

        return {
            "token_savings": token_savings,
            "total_tokens": total_used,
            "avg_processing_ms": avg_processing,
            "avg_quality_score": avg_quality,
            "resource_samples": len(self.resource_usage),
            "artifact_ingestions": len(self.artifact_ingestions),
            "artifact_queries": len(self.artifact_queries),
        }

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _memory_mb(self) -> float:
        try:
            if PSUTIL_AVAILABLE:
                return psutil.Process(os.getpid()).memory_info().rss / 1024 / 1024
            elif RESOURCE_AVAILABLE:
                usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
                if os.name == "posix" and not os.uname().sysname.lower().startswith("darwin"):
                    return usage / 1024
                return usage / 1024 / 1024
            else:
                return 0.0
        except Exception:  # pragma: no cover - fallback
            return 0.0

    def _cpu_percent(self) -> float:
        try:
            if PSUTIL_AVAILABLE:
                return psutil.cpu_percent(interval=None)
            else:
                return 0.0
        except Exception:  # pragma: no cover - fallback
            return 0.0
