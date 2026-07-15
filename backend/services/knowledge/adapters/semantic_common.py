"""Shared semantic-observation extraction helpers for deterministic adapters.

This module centralizes strict semantic row validation so adapters can apply a
consistent semantic-first policy before metadata/artifact fallback logic.
"""

from __future__ import annotations

from typing import Mapping, Sequence

from ..contracts import ObservationCreate
from .base import AdapterContext
from .web_common import dedupe_observations, make_observation


def extract_semantic_observations(
    context: AdapterContext,
    *,
    allowed_subject_types_by_observation: Mapping[str, set[str]],
) -> list[ObservationCreate]:
    """Build canonical observations from semantic rows that pass strict validation."""
    observations: list[ObservationCreate] = []
    for item in context.semantic_observations:
        if not isinstance(item, Mapping):
            continue

        observation_type = str(item.get("observation_type") or "").strip().lower()
        subject_type = str(item.get("subject_type") or "").strip().lower()
        subject_key = str(item.get("subject_key") or "").strip().lower()
        allowed_subject_types = allowed_subject_types_by_observation.get(observation_type)
        if not allowed_subject_types:
            continue
        if subject_type not in allowed_subject_types:
            continue
        if not subject_key:
            continue

        payload = item.get("payload")
        observations.append(
            make_observation(
                context=context,
                observation_type=observation_type,
                subject_type=subject_type,
                subject_key=subject_key,
                payload=payload if isinstance(payload, Mapping) else {},
            )
        )
    return dedupe_observations(observations)


__all__: Sequence[str] = ("extract_semantic_observations",)
