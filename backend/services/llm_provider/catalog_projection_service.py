"""Project owner-scoped LLM catalog data into transport-neutral outcomes.

This service owns ordered, read-only catalog composition over existing provider
authorities. It excludes HTTP schemas, migration and transaction side effects,
credential resolution, guarded transport, and public route adaptation.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from sqlalchemy.orm import Session

from backend.models import LLMInferenceConnection, LLMModelDeployment

from .application_contracts import (
    CatalogModelOutcome,
    CatalogOutcome,
    CatalogProviderOutcome,
    ConnectionCatalogMetadataOutcome,
    ConnectionConfigFieldOutcome,
    MaskedCredentialStatusOutcome,
    ProvingCatalogMetadataOutcome,
    RunnabilityOutcome,
    VerificationOutcome,
)
from .catalog_service import CatalogModelSummary, CatalogProviderSummary
from .connection_service import LLMConnectionService
from .connection_status_service import LLMConnectionStatusService
from .deployment_service import LLMDeploymentService
from .operation_registry import (
    GPT_OSS_20B_PROVING_PRESET_ID,
    PUBLIC_GPT_OSS_20B_PRESET_IDS,
    ConnectionOperationRegistry,
    OperationRegistryError,
    ProvingConnectionPreset,
)
from .types import (
    CredentialStatus,
    LLMConnectionState,
    LLMDeploymentValidationError,
    ProviderConfigurationError,
)

_GPT_OSS_CATALOG_MODEL_ID = "gpt-oss-20b"
_GPT_OSS_PROVING_WIRE_MODEL_ID = "openai/gpt-oss-20b"


class LLMCatalogProjectionService:
    """Build ordered catalog outcomes without workflow side effects."""

    def __init__(self, db: Session) -> None:
        self._connections = LLMConnectionService(db)
        self._deployments = LLMDeploymentService(db)
        self._status = LLMConnectionStatusService(db)
        self._registry = ConnectionOperationRegistry()

    def project(
        self,
        *,
        user_id: int,
        providers: Sequence[CatalogProviderSummary],
        credential_statuses: Mapping[str, CredentialStatus],
    ) -> CatalogOutcome:
        """Return static and reviewed providers in the active catalog order."""

        deployment_map = self._owner_deployment_map(user_id=user_id)
        static_providers = tuple(
            self._static_provider(
                user_id=user_id,
                provider=provider,
                credential_status=credential_statuses[provider.id],
                deployment_map=deployment_map,
            )
            for provider in providers
        )
        reviewed_providers = self._reviewed_connection_providers(user_id=user_id)
        return CatalogOutcome(providers=static_providers + reviewed_providers)

    def _owner_deployment_map(
        self,
        *,
        user_id: int,
    ) -> dict[tuple[str, str], tuple[LLMInferenceConnection, LLMModelDeployment]]:
        mapped: dict[
            tuple[str, str],
            tuple[LLMInferenceConnection, LLMModelDeployment],
        ] = {}
        for connection in self._connections.list_for_user(user_id=user_id):
            if connection.legacy_default_provider is None:
                continue
            for deployment in self._deployments.list_deployments(
                user_id=user_id,
                connection_id=connection.id,
            ):
                model = deployment.canonical_model_id or deployment.wire_model_id
                mapped[(connection.connection_preset_id, model.strip().lower())] = (
                    connection,
                    deployment,
                )
        return mapped

    def _static_provider(
        self,
        *,
        user_id: int,
        provider: CatalogProviderSummary,
        credential_status: CredentialStatus,
        deployment_map: dict[
            tuple[str, str],
            tuple[LLMInferenceConnection, LLMModelDeployment],
        ],
    ) -> CatalogProviderOutcome:
        credential = self._masked_credential_status(
            user_id=user_id,
            status=credential_status,
        )
        models = tuple(
            self._static_model(
                user_id=user_id,
                provider_id=provider.id,
                model=model,
                credential_runnable=(credential.enabled and credential.has_api_key),
                deployment_map=deployment_map,
            )
            for model in provider.models
        )
        return CatalogProviderOutcome(
            id=provider.id,
            label=provider.label,
            capabilities=provider.capabilities,
            available=provider.available,
            selectable=provider.selectable,
            credential=credential,
            models=models,
            default_model=provider.default_model,
        )

    def _masked_credential_status(
        self,
        *,
        user_id: int,
        status: CredentialStatus,
    ) -> MaskedCredentialStatusOutcome:
        if status.user_id != user_id:
            raise ProviderConfigurationError(
                "Credential status owner does not match catalog owner"
            )
        connection_ref = None
        if status.connection_id is not None:
            connection = self._connections.get_owned(
                user_id=status.user_id,
                connection_id=status.connection_id,
            )
            connection_ref = self._status.connection_ref(connection)
        return MaskedCredentialStatusOutcome(
            user_id=status.user_id,
            provider=status.provider,
            enabled=status.enabled,
            has_api_key=status.has_api_key,
            masked_api_key="***" if status.masked_api_key == "***" else None,
            connection_ref=connection_ref,
            auth_mode=status.auth_mode.value if status.auth_mode is not None else None,
        )

    def _static_model(
        self,
        *,
        user_id: int,
        provider_id: str,
        model: CatalogModelSummary,
        credential_runnable: bool,
        deployment_map: dict[
            tuple[str, str],
            tuple[LLMInferenceConnection, LLMModelDeployment],
        ],
    ) -> CatalogModelOutcome:
        target = deployment_map.get((provider_id, model.id))
        connection = target[0] if target is not None else None
        deployment = target[1] if target is not None else None
        runnable = bool(
            credential_runnable
            and connection is not None
            and connection.state == LLMConnectionState.ENABLED.value
            and deployment is not None
            and deployment.enabled
            and deployment.lifecycle_state == "active"
        )
        return CatalogModelOutcome(
            id=model.id,
            canonical_model_id=model.canonical_model_id,
            exact_wire_model_id=model.exact_wire_model_id,
            label=model.label,
            api_surface=model.api_surface,
            capabilities=model.capabilities,
            context_window_tokens=model.context_window_tokens,
            max_output_tokens=model.max_output_tokens,
            reasoning_efforts=model.reasoning_efforts,
            visible_reasoning_efforts=model.visible_reasoning_efforts,
            default_reasoning_effort=model.default_reasoning_effort,
            default_visible_reasoning_effort=model.default_visible_reasoning_effort,
            tool_choice_modes=model.tool_choice_modes,
            structured_output_strategies=model.structured_output_strategies,
            pricing_status=model.pricing_status,
            deployment_ref=(
                self._status.deployment_ref(deployment)
                if deployment is not None
                else None
            ),
            runnable=runnable,
            connection=None,
            proving=self._proving_metadata(
                user_id=user_id,
                model_id=model.id,
                deployment_map=deployment_map,
            ),
        )

    def _reviewed_connection_providers(
        self,
        *,
        user_id: int,
    ) -> tuple[CatalogProviderOutcome, ...]:
        connections = self._connections.list_for_user(user_id=user_id)
        by_preset: dict[str, list[LLMInferenceConnection]] = {}
        for connection in connections:
            by_preset.setdefault(connection.connection_preset_id, []).append(connection)

        rows: list[CatalogProviderOutcome] = []
        for preset_id in self._registry.list_public_gpt_oss_20b_preset_ids():
            try:
                preset = self._registry.get_connection_preset(preset_id)
            except OperationRegistryError:
                continue
            preset_connections = by_preset.get(preset.id, [])
            models: list[CatalogModelOutcome] = []
            for connection in preset_connections:
                owned_deployments = tuple(
                    deployment
                    for deployment in self._deployments.list_deployments(
                        user_id=user_id,
                        connection_id=connection.id,
                    )
                    if self._deployment_matches_product_preset(preset, deployment)
                )
                if not owned_deployments:
                    models.append(
                        self._connection_model(
                            user_id=user_id,
                            preset=preset,
                            connection=connection,
                            deployment=None,
                        )
                    )
                    continue
                models.extend(
                    self._connection_model(
                        user_id=user_id,
                        preset=preset,
                        connection=connection,
                        deployment=deployment,
                    )
                    for deployment in owned_deployments
                )
            if not models:
                models.append(
                    self._connection_model(
                        user_id=user_id,
                        preset=preset,
                        connection=None,
                        deployment=None,
                    )
                )
            if preset.id in PUBLIC_GPT_OSS_20B_PRESET_IDS:
                models = [max(models, key=self._product_catalog_model_score)]
            model_outcomes = tuple(models)
            rows.append(
                CatalogProviderOutcome(
                    id=preset.id,
                    label=self._connection_provider_label(preset),
                    capabilities=tuple(
                        sorted(
                            capability.value
                            for capability in preset.capability_ceiling
                        )
                    ),
                    available=True,
                    selectable=True,
                    credential=MaskedCredentialStatusOutcome(
                        user_id=user_id,
                        provider=preset.id,
                        enabled=any(
                            connection.state == LLMConnectionState.ENABLED.value
                            for connection in preset_connections
                        ),
                        has_api_key=bool(preset_connections),
                        masked_api_key=None,
                        connection_ref=(
                            self._status.connection_ref(preset_connections[0])
                            if preset_connections
                            else None
                        ),
                        auth_mode="bearer",
                    ),
                    models=model_outcomes,
                    default_model=model_outcomes[0].id,
                )
            )
        return tuple(rows)

    @staticmethod
    def _product_catalog_model_score(
        model: CatalogModelOutcome,
    ) -> tuple[int, int, int]:
        metadata = model.connection
        return (
            int(model.runnable),
            int(model.deployment_ref is not None),
            int(metadata is not None and metadata.lifecycle_state == "enabled"),
        )

    @staticmethod
    def _deployment_matches_product_preset(
        preset: ProvingConnectionPreset,
        deployment: LLMModelDeployment,
    ) -> bool:
        if preset.id not in PUBLIC_GPT_OSS_20B_PRESET_IDS:
            return False
        if preset.exact_wire_model_id:
            return deployment.wire_model_id == preset.exact_wire_model_id
        canonical_model_id = deployment.canonical_model_id or deployment.wire_model_id
        return canonical_model_id == preset.canonical_model_id

    def _connection_model(
        self,
        *,
        user_id: int,
        preset: ProvingConnectionPreset,
        connection: LLMInferenceConnection | None,
        deployment: LLMModelDeployment | None,
    ) -> CatalogModelOutcome:
        route = None
        if deployment is not None:
            try:
                route = self._status.first_route_for_deployment(
                    user_id=user_id,
                    deployment_id=deployment.id,
                )
            except LLMDeploymentValidationError:
                route = None
        runnability = self._status.connection_runnability(
            user_id=user_id,
            connection=connection,
            deployment=deployment,
            route=route,
        )
        wire_model_id = (
            (deployment.wire_model_id if deployment is not None else None)
            or preset.exact_wire_model_id
            or None
        )
        model_id = wire_model_id or preset.id
        label = (
            f"GPT-OSS 20B via {self._connection_provider_label(preset)}"
            if preset.id in PUBLIC_GPT_OSS_20B_PRESET_IDS
            else (
                deployment.display_name
                if deployment is not None
                else preset.display_name
            )
        )
        deployment_ref = (
            self._status.deployment_ref(deployment)
            if deployment is not None
            else None
        )
        return CatalogModelOutcome(
            id=model_id,
            canonical_model_id=(
                (deployment.canonical_model_id if deployment is not None else None)
                or preset.canonical_model_id
                or model_id
            ),
            exact_wire_model_id=wire_model_id,
            label=label,
            api_surface=preset.api_surface,
            capabilities=tuple(
                sorted(capability.value for capability in preset.capability_ceiling)
            ),
            context_window_tokens=128000,
            max_output_tokens=10000,
            reasoning_efforts=(),
            visible_reasoning_efforts=(),
            default_reasoning_effort=None,
            default_visible_reasoning_effort=None,
            tool_choice_modes=("auto",),
            structured_output_strategies=(),
            pricing_status="unavailable",
            deployment_ref=deployment_ref,
            runnable=runnability.runnable,
            connection=self._connection_metadata(
                preset=preset,
                connection=connection,
                deployment=deployment,
                runnability=runnability,
            ),
            proving=None,
        )

    def _connection_metadata(
        self,
        *,
        preset: ProvingConnectionPreset,
        connection: LLMInferenceConnection | None,
        deployment: LLMModelDeployment | None,
        runnability: RunnabilityOutcome,
    ) -> ConnectionCatalogMetadataOutcome:
        fields = self._connection_config_fields(
            preset,
            needs_wire_model=(
                deployment is None and preset.endpoint_config_field == "base_url"
            ),
        )
        return ConnectionCatalogMetadataOutcome(
            preset_id=preset.id,
            display_name=preset.display_name,
            enabled=True,
            auth_mode=preset.auth_mode,
            user_config_fields=tuple(field.name for field in fields),
            config_fields=fields,
            lifecycle_state=(
                connection.state if connection is not None else "not_created"
            ),
            connection_ref=(
                self._status.connection_ref(connection)
                if connection is not None
                else None
            ),
            deployment_ref=(
                self._status.deployment_ref(deployment)
                if deployment is not None
                else None
            ),
            verification=self._status.not_tested_verification(),
            runnability=runnability,
        )

    @staticmethod
    def _connection_config_fields(
        preset: ProvingConnectionPreset,
        *,
        needs_wire_model: bool,
    ) -> tuple[ConnectionConfigFieldOutcome, ...]:
        fields: list[ConnectionConfigFieldOutcome] = []
        for name in preset.user_config_fields:
            if name == "display_label":
                continue
            if name == "api_key":
                fields.append(
                    ConnectionConfigFieldOutcome(
                        name="api_key",
                        label="API key",
                        field_type="password",
                        required=True,
                        secret=True,
                    )
                )
            elif name == "base_url":
                fields.append(
                    ConnectionConfigFieldOutcome(
                        name="base_url",
                        label="Base URL",
                        field_type="url",
                        required=True,
                        secret=False,
                    )
                )
        if needs_wire_model:
            fields.append(
                ConnectionConfigFieldOutcome(
                    name="wire_model_id",
                    label="Wire model ID",
                    field_type="text",
                    required=True,
                    secret=False,
                )
            )
        return tuple(fields)

    @staticmethod
    def _connection_provider_label(preset: ProvingConnectionPreset) -> str:
        labels = {
            "huggingface": "Hugging Face",
            "nvidia_nim": "NVIDIA NIM",
            "ollama_compatible": "Ollama",
            "vllm": "vLLM",
            "organization_managed": "Custom OpenAI-compatible",
        }
        return labels.get(preset.serving_operator_id, preset.display_name)

    def _proving_metadata(
        self,
        *,
        user_id: int,
        model_id: str,
        deployment_map: dict[
            tuple[str, str],
            tuple[LLMInferenceConnection, LLMModelDeployment],
        ],
    ) -> ProvingCatalogMetadataOutcome | None:
        if model_id != _GPT_OSS_CATALOG_MODEL_ID:
            return None
        preset = self._registry.get_proving_preset(GPT_OSS_20B_PROVING_PRESET_ID)
        not_tested = self._status.not_tested_verification()
        target = deployment_map.get(
            (GPT_OSS_20B_PROVING_PRESET_ID, _GPT_OSS_PROVING_WIRE_MODEL_ID)
        )
        if target is None:
            return ProvingCatalogMetadataOutcome(
                preset_id=preset.id,
                display_name=preset.display_name,
                enabled=True,
                auth_mode=preset.auth_mode,
                user_config_fields=preset.user_config_fields,
                lifecycle_state="not_created",
                connection_ref=None,
                deployment_ref=None,
                verification=not_tested,
                runnability=RunnabilityOutcome(
                    status="capability_unknown",
                    selectable=True,
                    runnable=False,
                    reason="Usage evidence is required.",
                ),
            )
        connection, deployment = target
        route = self._status.first_route_for_deployment(
            user_id=user_id,
            deployment_id=deployment.id,
        )
        runnability = self._status.proving_runnability(
            connection=connection,
            deployment=deployment,
            route=route,
        )
        verification = (
            VerificationOutcome(
                status="passed",
                code="verified",
                message="GPT-OSS proving endpoint verified",
                retryable=False,
                model_present=True,
            )
            if runnability.runnable
            else not_tested
        )
        return ProvingCatalogMetadataOutcome(
            preset_id=preset.id,
            display_name=preset.display_name,
            enabled=True,
            auth_mode=preset.auth_mode,
            user_config_fields=preset.user_config_fields,
            lifecycle_state=connection.state,
            connection_ref=self._status.connection_ref(connection),
            deployment_ref=self._status.deployment_ref(deployment),
            verification=verification,
            runnability=runnability,
        )


__all__ = ["LLMCatalogProjectionService"]
