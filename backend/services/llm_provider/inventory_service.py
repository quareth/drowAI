"""Bounded inventory and capability verification for LLM deployments.

This module owns deployment-scoped probing that runs through guarded
operations, records capability observations, and returns sanitized result
metadata without mutating the process-global model profile registry.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from agent.providers.llm.core.capabilities import LLMCapability
from backend.models import LLMCapabilityObservation, LLMDeploymentRoute, LLMModelDeployment

from .connection_authorization import LLMConnectionAuthorizer
from .deployment_service import LLMDeploymentService
from .effective_profile_service import EffectiveProfileService
from .guarded_transport import GuardedTransport, GuardedTransportError
from .operation_registry import GPT_OSS_20B_PROVING_PRESET_ID, ConnectionOperationRegistry
from .types import (
    LLMConnectionAccessContext,
    LLMConnectionAuthorizationError,
    LLMConnectionOperation,
    ProviderSecret,
)

_OBSERVATION_SOURCE = "gpt_oss_proving_probe"
_OBSERVATION_TTL = timedelta(hours=1)


@dataclass(frozen=True, slots=True)
class GptOssProvingVerificationResult:
    """Sanitized verification output safe for API and UI surfaces."""

    status: str
    code: str
    message: str
    retryable: bool
    observed_at: datetime
    expires_at: datetime
    model_present: bool | None = None
    usage: dict[str, int] | None = None


class LLMProviderInventoryService:
    """Verify deployment inventory and minimal capabilities through guarded egress."""

    def __init__(
        self,
        db: Session,
        *,
        guarded_transport: GuardedTransport | None = None,
        connection_authorizer: LLMConnectionAuthorizer | None = None,
        operation_registry: ConnectionOperationRegistry | None = None,
    ) -> None:
        self._db = db
        self._guarded_transport = guarded_transport or GuardedTransport()
        self._connection_authorizer = connection_authorizer or LLMConnectionAuthorizer(db)
        self._operation_registry = operation_registry or ConnectionOperationRegistry()
        self._deployments = LLMDeploymentService(db)

    def verify_gpt_oss_20b_proving_connection(
        self,
        *,
        user_id: int,
        connection_id: UUID | str,
        expected_connection_revision: int,
        deployment_id: UUID | str,
        route_id: UUID | str,
        api_key: str,
        credential_fingerprint: str,
    ) -> GptOssProvingVerificationResult:
        """Run the minimal GPT-OSS proving inventory and chat-usage probe."""

        observed_at = _now()
        if not isinstance(api_key, str) or not api_key.strip():
            return _result(
                status="failed",
                code="auth_missing",
                message="Proving credential is unavailable",
                retryable=False,
                observed_at=observed_at,
            )
        try:
            deployment = self._deployments.get_deployment(
                user_id=user_id,
                deployment_id=deployment_id,
            )
            route = self._deployments.get_route(user_id=user_id, route_id=route_id)
            if str(deployment.connection_id) != str(connection_id) or route.deployment_id != deployment.id:
                return _result(
                    status="failed",
                    code="deployment_route_mismatch",
                    message="Deployment route is unavailable",
                    retryable=False,
                    observed_at=observed_at,
                )
            EffectiveProfileService(self._db).resolve(
                connection=self._connection_authorized_for_probe(
                    user_id=user_id,
                    connection_id=connection_id,
                    expected_revision=expected_connection_revision,
                    operation=LLMConnectionOperation.INVENTORY,
                ),
                deployment=deployment,
                route=route,
            )
        except (LLMConnectionAuthorizationError, ValueError, TypeError):
            return _result(
                status="failed",
                code="authorization_failed",
                message="Connection is not authorized for verification",
                retryable=False,
                observed_at=observed_at,
            )

        preset = self._operation_registry.get_proving_preset(GPT_OSS_20B_PROVING_PRESET_ID)
        inventory = self._execute_json(
            operation=LLMConnectionOperation.INVENTORY,
            api_key=api_key,
        )
        if inventory is None:
            return _result(
                status="failed",
                code="inventory_unavailable",
                message="Provider inventory evidence is unavailable",
                retryable=True,
                observed_at=observed_at,
            )
        if preset.exact_wire_model_id not in _inventory_model_ids(inventory):
            return _result(
                status="failed",
                code="model_not_found",
                message="Exact proving model alias was not present in inventory",
                retryable=False,
                observed_at=observed_at,
                model_present=False,
            )

        try:
            self._connection_authorized_for_probe(
                user_id=user_id,
                connection_id=connection_id,
                expected_revision=expected_connection_revision,
                operation=LLMConnectionOperation.CAPABILITY_PROBE,
            )
        except LLMConnectionAuthorizationError:
            return _result(
                status="failed",
                code="authorization_failed",
                message="Connection is not authorized for verification",
                retryable=False,
                observed_at=observed_at,
                model_present=True,
            )
        probe = self._execute_json(
            operation=LLMConnectionOperation.CAPABILITY_PROBE,
            api_key=api_key,
            json_body={
                "model": preset.exact_wire_model_id,
                "messages": [{"role": "user", "content": "ping"}],
                "stream": False,
                "max_tokens": 1,
            },
        )
        usage = _usage(probe)
        if usage is None:
            return _result(
                status="failed",
                code="usage_unavailable",
                message="Provider usage evidence is unavailable",
                retryable=False,
                observed_at=observed_at,
                model_present=True,
            )

        self._record_supported_observations(
            deployment=deployment,
            route=route,
            connection_id=str(connection_id),
            connection_revision=expected_connection_revision,
            credential_fingerprint=credential_fingerprint,
            capabilities=(
                LLMCapability.CHAT,
                LLMCapability.USAGE_REPORTING,
            ),
            observed_at=observed_at,
        )
        return _result(
            status="passed",
            code="verified",
            message="GPT-OSS proving endpoint verified",
            retryable=False,
            observed_at=observed_at,
            model_present=True,
            usage=usage,
        )

    def _connection_authorized_for_probe(
        self,
        *,
        user_id: int,
        connection_id: UUID | str,
        expected_revision: int,
        operation: LLMConnectionOperation,
    ):
        self._connection_authorizer.authorize(
            access_context=LLMConnectionAccessContext(authenticated_user_id=user_id),
            connection_id=connection_id,
            expected_revision=expected_revision,
            operation=operation,
        )
        from backend.models import LLMInferenceConnection

        connection = self._db.get(LLMInferenceConnection, UUID(str(connection_id)))
        if connection is None:
            raise LLMConnectionAuthorizationError(
                code="connection_unavailable",
                message="Connection is unavailable",
            )
        return connection

    def _execute_json(
        self,
        *,
        operation: LLMConnectionOperation,
        api_key: str,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        try:
            response = self._guarded_transport.execute(
                operation,
                provider=GPT_OSS_20B_PROVING_PRESET_ID,
                secret=ProviderSecret(
                    provider=GPT_OSS_20B_PROVING_PRESET_ID,
                    value=api_key,
                ),
                **({"json_body": json_body} if json_body is not None else {}),
            )
            payload = json.loads(response.body)
        except (GuardedTransportError, TypeError, ValueError, UnicodeDecodeError):
            return None
        return payload if isinstance(payload, dict) else None

    def _record_supported_observations(
        self,
        *,
        deployment: LLMModelDeployment,
        route: LLMDeploymentRoute,
        connection_id: str,
        connection_revision: int,
        credential_fingerprint: str,
        capabilities: tuple[LLMCapability, ...],
        observed_at: datetime,
    ) -> None:
        constraints = {
            "connection_id": str(connection_id),
            "connection_revision": int(connection_revision),
            "credential_fingerprint": str(credential_fingerprint),
        }
        for capability in capabilities:
            revision = _next_observation_revision(
                self._db,
                deployment_id=deployment.id,
                route_id=route.id,
                capability=capability.value,
            )
            fingerprint = _fingerprint(
                deployment_id=str(deployment.id),
                route_id=str(route.id),
                capability=capability.value,
                revision=revision,
            )
            self._db.add(
                LLMCapabilityObservation(
                    id=uuid4(),
                    deployment_id=deployment.id,
                    route_id=route.id,
                    capability=capability.value,
                    support_state="supported",
                    constraints=constraints,
                    source=_OBSERVATION_SOURCE,
                    observed_at=observed_at,
                    expires_at=observed_at + _OBSERVATION_TTL,
                    revision=revision,
                    fingerprint=fingerprint,
                )
            )
        self._db.flush()


def _result(
    *,
    status: str,
    code: str,
    message: str,
    retryable: bool,
    observed_at: datetime,
    model_present: bool | None = None,
    usage: dict[str, int] | None = None,
) -> GptOssProvingVerificationResult:
    return GptOssProvingVerificationResult(
        status=status,
        code=code,
        message=message,
        retryable=retryable,
        observed_at=observed_at,
        expires_at=observed_at + _OBSERVATION_TTL,
        model_present=model_present,
        usage=usage,
    )


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _inventory_model_ids(payload: dict[str, Any]) -> frozenset[str]:
    data = payload.get("data")
    if not isinstance(data, list):
        return frozenset()
    ids = []
    for item in data:
        if isinstance(item, dict) and isinstance(item.get("id"), str):
            ids.append(item["id"])
    return frozenset(ids)


def _usage(payload: dict[str, Any] | None) -> dict[str, int] | None:
    if payload is None or not isinstance(payload.get("usage"), dict):
        return None
    usage = payload["usage"]
    values = {}
    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
        value = usage.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return None
        values[key] = value
    return values


def _next_observation_revision(
    db: Session,
    *,
    deployment_id,
    route_id,
    capability: str,
) -> int:
    current = db.execute(
        select(func.max(LLMCapabilityObservation.revision)).where(
            LLMCapabilityObservation.deployment_id == deployment_id,
            LLMCapabilityObservation.route_id == route_id,
            LLMCapabilityObservation.capability == capability,
        )
    ).scalar_one()
    return int(current or 0) + 1


def _fingerprint(
    *,
    deployment_id: str,
    route_id: str,
    capability: str,
    revision: int,
) -> str:
    material = f"{deployment_id}:{route_id}:{capability}:{revision}".encode("utf-8")
    return hashlib.sha256(material).hexdigest()


__all__ = [
    "GptOssProvingVerificationResult",
    "LLMProviderInventoryService",
]
