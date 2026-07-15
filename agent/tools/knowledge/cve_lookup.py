"""Standalone runtime tool wrapper for product-version CVE lookup.

Scope:
- Accepts only concrete `product` and `version` input.
- Evaluates CVE index availability and projection coverage.
- Delegates deterministic matching to backend `CveMatchService`.

Boundary:
- Contains no task/engagement/service-inventory coupling.
- Contains no SQL matching logic or version evaluation internals.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..base_tool import BaseTool
from ..schemas import ToolResult

logger = logging.getLogger(__name__)


class CveLookupArgs(BaseModel):
    """Arguments for standalone `knowledge.cve_lookup`."""

    model_config = ConfigDict(extra="forbid")

    product: str = Field(..., description="Concrete product identity to look up.")
    version: str = Field(..., description="Concrete product version to evaluate.")
    max_results: int = Field(
        5,
        ge=1,
        le=25,
        description="Maximum number of returned CVE matches.",
    )

    @field_validator("product", "version")
    @classmethod
    def _validate_non_empty_text(cls, value: str) -> str:
        cleaned = str(value or "").strip()
        if not cleaned:
            raise ValueError("value is required")
        return cleaned


class CveLookupTool(BaseTool):
    """Resolve CVE candidates for one standalone product-version request."""

    args_model = CveLookupArgs

    def run(self, args: CveLookupArgs) -> ToolResult:
        start = time.time()

        try:
            from backend.database import SessionLocal
            from backend.services.metrics.utils import safe_inc
            from backend.services.cve_indexing.match_contracts import CveLookupRequest
            from backend.services.cve_indexing.match_service import CveMatchService
        except Exception as exc:  # pragma: no cover - import safety
            return self._error_result(
                exit_code=1,
                args=args,
                reason="backend_import_error",
                message=f"knowledge.cve_lookup failed to initialize backend services: {exc}",
                start=start,
            )

        db = SessionLocal()
        try:
            readiness_start = time.perf_counter()
            lookup_state = self._resolve_lookup_state(db)
            readiness_ms = max(0.0, (time.perf_counter() - readiness_start) * 1000.0)
            self._emit_phase_timing("readiness_eval", readiness_ms)
            availability = dict(lookup_state["availability"])
            coverage = dict(lookup_state["coverage"])
            if not bool(availability.get("available")):
                safe_inc("cve.lookup.unavailable_total")
                safe_inc(self._unavailable_reason_metric(str(availability.get("reason"))))
                return self._error_result(
                    exit_code=2,
                    args=args,
                    reason=str(availability.get("reason") or "lookup_unavailable"),
                    message=str(availability.get("message") or "knowledge.cve_lookup is unavailable."),
                    start=start,
                    extra_metadata={
                        "availability": availability,
                        "coverage": coverage,
                    },
                )

            lookup_request = CveLookupRequest(
                product=args.product,
                version=args.version,
                max_results=args.max_results,
            )
            lookup_start = time.perf_counter()
            response = CveMatchService(db).lookup(lookup_request)
            match_lookup_ms = max(0.0, (time.perf_counter() - lookup_start) * 1000.0)
            self._emit_phase_timing("match_lookup", match_lookup_ms)
        except ValueError as exc:
            return self._error_result(
                exit_code=2,
                args=args,
                reason="validation_failed",
                message=f"knowledge.cve_lookup rejected input: {exc}",
                start=start,
            )
        except Exception as exc:
            return self._error_result(
                exit_code=1,
                args=args,
                reason="lookup_error",
                message=f"knowledge.cve_lookup failed: {exc}",
                start=start,
            )
        finally:
            db.close()

        payload = self._build_output_payload(response=response, coverage=coverage)
        audit = self._build_audit_summary(payload=payload)
        return ToolResult(
            success=True,
            exit_code=0,
            stdout=json.dumps(payload, ensure_ascii=True),
            stderr="",
            artifacts=[],
            metadata={
                "cve_lookup": {
                    "status": payload["status"],
                    "request": args.model_dump(),
                    "evidence_contract_version": "2.0",
                    "availability": availability,
                    "coverage": coverage,
                    "audit": audit,
                    "timings_ms": {
                        "readiness_eval": readiness_ms,
                        "match_lookup": match_lookup_ms,
                    },
                    "response": payload,
                }
            },
            execution_time=max(0.0, time.time() - start),
        )

    @staticmethod
    def _error_result(
        *,
        exit_code: int,
        args: CveLookupArgs,
        reason: str,
        message: str,
        start: float,
        extra_metadata: dict[str, Any] | None = None,
    ) -> ToolResult:
        metadata_payload = {
            "status": reason,
            "request": args.model_dump(),
        }
        if isinstance(extra_metadata, dict):
            metadata_payload.update(extra_metadata)
        return ToolResult(
            success=False,
            exit_code=exit_code,
            stdout="",
            stderr=message,
            artifacts=[],
            metadata={"cve_lookup": metadata_payload},
            execution_time=max(0.0, time.time() - start),
        )

    @staticmethod
    def _resolve_lookup_state(db: Any) -> dict[str, Any]:
        """Return lookup availability + coverage with best-effort partial support."""
        try:
            from backend.config.feature_flags import is_knowledge_cve_lookup_enabled
            from backend.models.cve import CveIndexSettings
            from backend.services.cve_indexing.projection_readiness import CveProjectionReadinessService
        except Exception:
            return {
                "availability": {
                    "available": False,
                    "reason": "availability_check_failed",
                    "message": "knowledge.cve_lookup availability check failed.",
                    "projection_ready": False,
                    "record_count": 0,
                    "affected_product_count": 0,
                    "status_counts": {},
                    "blocking_status_counts": {},
                    "blocking_reasons": [],
                },
                "coverage": {
                    "is_partial": False,
                    "pending_count": 0,
                    "error_count": 0,
                    "projected_count": 0,
                    "record_count": 0,
                    "warning": "",
                },
            }

        if not is_knowledge_cve_lookup_enabled():
            return {
                "availability": {
                    "available": False,
                    "reason": "lookup_disabled_by_feature_flag",
                    "message": "knowledge.cve_lookup is disabled by feature flag.",
                    "projection_ready": False,
                    "record_count": 0,
                    "affected_product_count": 0,
                    "status_counts": {},
                    "blocking_status_counts": {},
                    "blocking_reasons": [],
                },
                "coverage": {
                    "is_partial": False,
                    "pending_count": 0,
                    "error_count": 0,
                    "projected_count": 0,
                    "record_count": 0,
                    "warning": "",
                },
            }

        settings = db.query(CveIndexSettings).order_by(CveIndexSettings.id.asc()).first()
        if settings is not None and not bool(getattr(settings, "enabled", False)):
            return {
                "availability": {
                    "available": False,
                    "reason": "lookup_disabled_by_index_settings",
                    "message": "knowledge.cve_lookup is unavailable because CVE indexing is disabled.",
                    "projection_ready": False,
                    "record_count": 0,
                    "affected_product_count": 0,
                    "status_counts": {},
                    "blocking_status_counts": {},
                    "blocking_reasons": [],
                },
                "coverage": {
                    "is_partial": False,
                    "pending_count": 0,
                    "error_count": 0,
                    "projected_count": 0,
                    "record_count": 0,
                    "warning": "",
                },
            }

        readiness = CveProjectionReadinessService(db).evaluate()
        return {
            "availability": readiness.to_lookup_availability(allow_partial=True),
            "coverage": readiness.to_lookup_coverage(),
        }

    @staticmethod
    def _build_audit_summary(*, payload: dict[str, Any]) -> dict[str, Any]:
        matches = payload.get("matches")
        if not isinstance(matches, list):
            matches = []
        applicable_count = sum(
            1 for item in matches if isinstance(item, dict) and item.get("applicability") == "applicable"
        )
        possible_count = sum(
            1 for item in matches if isinstance(item, dict) and item.get("applicability") == "possible"
        )
        return {
            "status": payload.get("status"),
            "matches_returned": len(matches),
            "applicable_count": int(applicable_count),
            "possible_count": int(possible_count),
        }

    @staticmethod
    def _emit_phase_timing(phase: str, duration_ms: float) -> None:
        try:
            from backend.services.metrics.utils import safe_gauge

            safe_gauge(f"cve.lookup.timing_ms.{phase}", float(duration_ms))
        except Exception:
            pass
        logger.debug("knowledge.cve_lookup phase=%s duration_ms=%.3f", phase, float(duration_ms))

    @staticmethod
    def _unavailable_reason_metric(reason: str) -> str:
        normalized = str(reason or "").strip().lower()
        allowed = {
            "lookup_disabled_by_feature_flag",
            "lookup_disabled_by_index_settings",
            "lookup_index_empty",
            "availability_check_failed",
        }
        if normalized not in allowed:
            normalized = "other"
        return f"cve.lookup.unavailable.{normalized}"

    @staticmethod
    def _build_output_payload(*, response: Any, coverage: dict[str, Any]) -> dict[str, Any]:
        matches_payload: list[dict[str, Any]] = []
        for item in tuple(getattr(response, "matches", ()) or ()):
            if bool(item.version_applicable):
                applicability = "applicable"
            elif item.version_applicable is None:
                applicability = "possible"
            else:
                applicability = "unknown"
            matches_payload.append(
                {
                    "cve_id": item.cve_id,
                    "applicability": applicability,
                    "match_reason": item.rationale,
                    "summary": item.summary,
                    "matched_fields": list(item.matched_fields),
                    "score": item.score,
                }
            )

        is_partial = bool(coverage.get("is_partial"))
        if is_partial:
            status = "partial_index"
        elif matches_payload:
            status = "ok"
        else:
            status = "no_matches"

        return {
            "tool": "knowledge.cve_lookup",
            "status": status,
            "coverage": dict(coverage),
            "message": str(getattr(response, "message", "") or ""),
            "matches": matches_payload,
        }


__all__ = ["CveLookupArgs", "CveLookupTool"]
