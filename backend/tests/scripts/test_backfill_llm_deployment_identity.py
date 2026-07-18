"""Tests for the deployment identity backfill operator script."""

from __future__ import annotations

import json
from uuid import uuid4

from backend.database import SessionLocal
from backend.models import User, UserLLMProviderCredential, UserLLMSelection
from backend.scripts.backfill_llm_deployment_identity import run_backfill


def test_script_reports_required_safe_counts_and_is_repeatable(
) -> None:
    """Operator output has stable counters and never includes ciphertext."""

    db = SessionLocal()
    try:
        owner = User(
            username=f"backfill-script-{uuid4().hex}",
            password="hashed",
        )
        db.add(owner)
        db.flush()
        ciphertext = "encrypted-secret-must-not-be-reported"
        db.add_all(
            [
                UserLLMProviderCredential(
                    user_id=owner.id,
                    provider="openai",
                    encrypted_api_key=ciphertext,
                    enabled=True,
                ),
                UserLLMSelection(
                    user_id=owner.id,
                    provider="openai",
                    model="gpt-5-mini",
                ),
            ]
        )
        db.flush()

        first = run_backfill(db=db)
        second = run_backfill(db=db)

        for result in (first, second):
            payload = result.to_dict()
            assert {
                "ready",
                "created",
                "skipped",
                "unmapped",
                "auth_missing",
                "mapping_required",
                "missing_legacy_connections",
                "failed",
            }.issubset(payload)
            assert ciphertext not in json.dumps(payload, sort_keys=True)
        assert first.created > 0
        assert first.failed == 0
        assert second.created == 0
        assert second.failed == 0
        assert second.skipped > 0
    finally:
        db.rollback()
        db.close()
