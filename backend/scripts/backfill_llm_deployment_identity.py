#!/usr/bin/env python3
"""Backfill deterministic user-owned legacy LLM deployment identities.

The command reports aggregate safe counters only. It never decrypts, prints,
or logs credential material, model configuration payloads, or endpoint details.
"""

from __future__ import annotations

import json
import logging
import sys
from collections.abc import Sequence

if __name__ == "__main__" and __package__ is None:
    from pathlib import Path

    root = str(Path(__file__).resolve().parents[2])
    if root not in sys.path:
        sys.path.insert(0, root)

from sqlalchemy.orm import Session

from backend.database import SessionLocal
from backend.services.llm_provider.migration_service import (
    LLMDeploymentBackfillStats,
    LLMProviderMigrationService,
)

logger = logging.getLogger(__name__)


def run_backfill(*, db: Session) -> LLMDeploymentBackfillStats:
    """Run a retryable backfill without committing the caller's transaction."""

    return LLMProviderMigrationService(db).backfill_all_deployment_identity(
        continue_on_error=True
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Run the operator backfill and print one safe JSON summary."""

    if argv:
        raise ValueError("This backfill command accepts no arguments")
    db = SessionLocal()
    try:
        result = run_backfill(db=db)
        db.commit()
        print(json.dumps(result.to_dict(), sort_keys=True))
        return 1 if result.failed else 0
    except Exception:
        db.rollback()
        logger.error("LLM deployment identity backfill failed")
        failed = LLMDeploymentBackfillStats(failed=1)
        print(json.dumps(failed.to_dict(), sort_keys=True))
        return 1
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
