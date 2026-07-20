"""Load and validate the reviewed LLM catalog manifest.

This module owns local manifest parsing only. It builds immutable runtime
profile inputs from checked-in catalog data and never performs provider
documentation discovery or outbound network calls.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from types import MappingProxyType
from typing import Any, Iterable, Mapping

from ..contracts.structured_output_strategy import freeze_structured_output_strategies
from ..contracts.tool_contracts import freeze_tool_choice_modes
from ..core.capabilities import LLMCapability, freeze_capabilities
from ..core.identity import ProviderModelRef, normalize_model_id, normalize_provider_id

DEFAULT_MANIFEST_PATH = Path(__file__).with_name("catalog_manifest.json")
_SUPPORTED_SCHEMA_VERSION = 1
_ALLOWED_LIFECYCLES = frozenset({"active", "deprecated", "unavailable"})
_ALLOWED_SUPPORT_TIERS = frozenset({"mainstream", "proving", "compatibility"})


class CatalogManifestValidationError(ValueError):
    """Raised when the reviewed model catalog manifest is malformed."""


@dataclass(frozen=True, slots=True)
class CatalogModel:
    """One resolved model entry from the reviewed catalog manifest."""

    provider: str
    model: str
    canonical_model_id: str
    display_name: str
    api_surface: str
    lifecycle: str
    support_tier: str
    capabilities: frozenset[LLMCapability]
    context_window_tokens: int
    max_output_tokens: int
    listable: bool
    aliases: tuple[str, ...]
    pricing_schedule_ref: str
    pricing_provenance: str
    reasoning_efforts: frozenset[str] = frozenset()
    default_reasoning_effort: str | None = None
    tool_choice_modes: frozenset[str] = frozenset()
    structured_output_strategies: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class CatalogManifest:
    """Validated active catalog revision used to build runtime profiles."""

    schema_version: int
    active_revision: str
    last_known_good_revision: str
    models: tuple[CatalogModel, ...]

    def require_model(self, provider: str, model: str) -> CatalogModel:
        """Return a manifest model by normalized provider/model identity."""
        normalized = (
            normalize_provider_id(provider),
            normalize_model_id(model),
        )
        for catalog_model in self.models:
            if (catalog_model.provider, catalog_model.model) == normalized:
                return catalog_model
        raise CatalogManifestValidationError(
            f"Catalog model '{normalized[0]}/{normalized[1]}' is not present"
        )

    def model_ids(
        self,
        provider: str,
        *,
        listable: bool | None = None,
        api_surface: str | None = None,
        support_tier: str | None = None,
    ) -> tuple[str, ...]:
        """Return model ids filtered by manifest-owned metadata."""
        normalized_provider = normalize_provider_id(provider)
        normalized_surface = str(api_surface or "").strip().lower() or None
        normalized_tier = str(support_tier or "").strip().lower() or None
        models = [
            model.model
            for model in self.models
            if model.provider == normalized_provider
            and (listable is None or model.listable is listable)
            and (normalized_surface is None or model.api_surface == normalized_surface)
            and (normalized_tier is None or model.support_tier == normalized_tier)
        ]
        return tuple(models)


def load_catalog_manifest(path: str | Path = DEFAULT_MANIFEST_PATH) -> CatalogManifest:
    """Load the active catalog manifest, falling back to last-known-good data."""
    manifest_path = Path(path)
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise CatalogManifestValidationError(
            f"Unable to read catalog manifest '{manifest_path}'"
        ) from exc
    except json.JSONDecodeError as exc:
        raise CatalogManifestValidationError(
            f"Catalog manifest '{manifest_path}' is not valid JSON"
        ) from exc

    header = _validate_manifest_header(payload)
    try:
        return _manifest_for_revision(payload, header["active_revision"])
    except CatalogManifestValidationError as active_error:
        last_known_good = header["last_known_good_revision"]
        if last_known_good == header["active_revision"]:
            raise active_error
        try:
            return _manifest_for_revision(payload, last_known_good)
        except CatalogManifestValidationError:
            raise active_error


def build_model_profiles_from_manifest(
    manifest: CatalogManifest,
    model_profile_type: type[Any],
) -> tuple[Any, ...]:
    """Build registry ``ModelProfile`` objects from a validated manifest."""
    return tuple(
        model_profile_type(
            ref=ProviderModelRef(model.provider, model.model),
            display_name=model.display_name,
            api_surface=model.api_surface,
            capabilities=model.capabilities,
            context_window_tokens=model.context_window_tokens,
            max_output_tokens=model.max_output_tokens,
            listable=model.listable,
            reasoning_efforts=model.reasoning_efforts,
            default_reasoning_effort=model.default_reasoning_effort,
            tool_choice_modes=model.tool_choice_modes,
            structured_output_strategies=model.structured_output_strategies,
            canonical_model_id=model.canonical_model_id,
            lifecycle=model.lifecycle,
            support_tier=model.support_tier,
            aliases=model.aliases,
            pricing_schedule_ref=model.pricing_schedule_ref,
            pricing_provenance=model.pricing_provenance,
        )
        for model in manifest.models
    )


def _validate_manifest_header(payload: Any) -> Mapping[str, Any]:
    """Validate manifest-level schema and revision identity."""
    if not isinstance(payload, Mapping):
        raise CatalogManifestValidationError("Catalog manifest must be a JSON object")
    schema_version = payload.get("schema_version")
    if schema_version != _SUPPORTED_SCHEMA_VERSION:
        raise CatalogManifestValidationError(
            f"Unsupported catalog manifest schema_version '{schema_version}'"
        )
    active_revision = _require_text(payload, "active_revision")
    last_known_good_revision = _require_text(payload, "last_known_good_revision")
    revisions = payload.get("revisions")
    if not isinstance(revisions, list) or not revisions:
        raise CatalogManifestValidationError("Catalog manifest requires non-empty revisions")
    revision_ids = [
        _require_text(revision, "revision")
        for revision in revisions
        if isinstance(revision, Mapping)
    ]
    if len(revision_ids) != len(revisions) or len(set(revision_ids)) != len(revision_ids):
        raise CatalogManifestValidationError("Catalog manifest revisions must be unique objects")
    if active_revision not in revision_ids:
        raise CatalogManifestValidationError("Active catalog revision is not present")
    return MappingProxyType(
        {
            "schema_version": schema_version,
            "active_revision": active_revision,
            "last_known_good_revision": last_known_good_revision,
        }
    )


def _manifest_for_revision(payload: Mapping[str, Any], revision_id: str) -> CatalogManifest:
    """Validate and return one manifest revision."""
    header = _validate_manifest_header(payload)
    revision = _find_revision(payload["revisions"], revision_id)
    profile_templates = _profile_templates(revision.get("profiles", {}))
    models_payload = revision.get("models")
    if not isinstance(models_payload, list) or not models_payload:
        raise CatalogManifestValidationError(
            f"Catalog revision '{revision_id}' requires non-empty models"
        )
    models = tuple(
        _catalog_model_from_payload(model_payload, profile_templates)
        for model_payload in models_payload
    )
    identities = [(model.provider, model.model) for model in models]
    if len(set(identities)) != len(identities):
        raise CatalogManifestValidationError(
            f"Catalog revision '{revision_id}' contains duplicate model identities"
        )
    return CatalogManifest(
        schema_version=int(header["schema_version"]),
        active_revision=revision_id,
        last_known_good_revision=str(header["last_known_good_revision"]),
        models=models,
    )


def _find_revision(revisions: Iterable[Any], revision_id: str) -> Mapping[str, Any]:
    """Return a raw revision object by id."""
    for revision in revisions:
        if isinstance(revision, Mapping) and revision.get("revision") == revision_id:
            return revision
    raise CatalogManifestValidationError(
        f"Catalog revision '{revision_id}' is not present"
    )


def _profile_templates(payload: Any) -> Mapping[str, Mapping[str, Any]]:
    """Return validated reusable manifest-owned profile templates."""
    if payload is None:
        return MappingProxyType({})
    if not isinstance(payload, Mapping):
        raise CatalogManifestValidationError("Catalog revision profiles must be an object")
    templates: dict[str, Mapping[str, Any]] = {}
    for key, template in payload.items():
        template_key = str(key).strip()
        if not template_key:
            raise CatalogManifestValidationError("Catalog profile key cannot be empty")
        if not isinstance(template, Mapping):
            raise CatalogManifestValidationError(
                f"Catalog profile '{template_key}' must be an object"
            )
        templates[template_key] = MappingProxyType(dict(template))
    return MappingProxyType(templates)


def _catalog_model_from_payload(
    model_payload: Any,
    profile_templates: Mapping[str, Mapping[str, Any]],
) -> CatalogModel:
    """Validate one model entry after applying an optional profile template."""
    if not isinstance(model_payload, Mapping):
        raise CatalogManifestValidationError("Catalog model entries must be objects")
    payload = dict(model_payload)
    profile_key = str(payload.pop("profile", "") or "").strip()
    if profile_key:
        try:
            base = dict(profile_templates[profile_key])
        except KeyError as exc:
            raise CatalogManifestValidationError(
                f"Catalog model references unknown profile '{profile_key}'"
            ) from exc
        base.update(payload)
        payload = base

    provider = normalize_provider_id(_require_text(payload, "provider"))
    model = normalize_model_id(_require_text(payload, "model"))
    lifecycle = _require_text(payload, "lifecycle").lower()
    support_tier = _require_text(payload, "support_tier").lower()
    if lifecycle not in _ALLOWED_LIFECYCLES:
        raise CatalogManifestValidationError(f"Unsupported lifecycle '{lifecycle}'")
    if support_tier not in _ALLOWED_SUPPORT_TIERS:
        raise CatalogManifestValidationError(f"Unsupported support tier '{support_tier}'")
    context_window_tokens, max_output_tokens = _limits(payload.get("limits"))
    canonical_model_id = _canonical_model_id(
        _require_text(payload, "canonical_model_id"),
        provider=provider,
    )
    return CatalogModel(
        provider=provider,
        model=model,
        canonical_model_id=canonical_model_id,
        display_name=_require_text(payload, "display_name"),
        api_surface=_require_text(payload, "api_surface").lower(),
        lifecycle=lifecycle,
        support_tier=support_tier,
        capabilities=freeze_capabilities(_require_non_empty_sequence(payload, "capabilities")),
        context_window_tokens=context_window_tokens,
        max_output_tokens=max_output_tokens,
        listable=_require_bool(payload, "listable"),
        aliases=_string_tuple(payload.get("aliases", ()), "aliases"),
        pricing_schedule_ref=_require_text(payload, "pricing_schedule_ref"),
        pricing_provenance=_require_text(payload, "pricing_provenance"),
        reasoning_efforts=frozenset(
            str(value).strip().lower()
            for value in _optional_sequence(payload, "reasoning_efforts")
            if str(value).strip()
        ),
        default_reasoning_effort=_optional_text(payload, "default_reasoning_effort"),
        tool_choice_modes=freeze_tool_choice_modes(
            _optional_sequence(payload, "tool_choice_modes")
        ),
        structured_output_strategies=freeze_structured_output_strategies(
            _optional_sequence(payload, "structured_output_strategies")
        ),
    )


def _require_text(payload: Mapping[str, Any], key: str) -> str:
    """Return a required non-empty string field."""
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise CatalogManifestValidationError(f"Catalog field '{key}' must be non-empty text")
    return value.strip()


def _optional_text(payload: Mapping[str, Any], key: str) -> str | None:
    """Return an optional non-empty string field."""
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise CatalogManifestValidationError(f"Catalog field '{key}' must be text when set")
    return value.strip().lower()


def _canonical_model_id(value: str, *, provider: str) -> str:
    """Normalize manifest canonical IDs to provider/model slash form."""

    stripped = value.strip()
    colon_prefix = f"{provider}:"
    if stripped.startswith(colon_prefix):
        suffix = stripped[len(colon_prefix):].strip()
        if suffix:
            return f"{provider}/{suffix}"
    return stripped


def _require_bool(payload: Mapping[str, Any], key: str) -> bool:
    """Return a required boolean field."""
    value = payload.get(key)
    if not isinstance(value, bool):
        raise CatalogManifestValidationError(f"Catalog field '{key}' must be boolean")
    return value


def _limits(payload: Any) -> tuple[int, int]:
    """Return positive context and output token limits."""
    if not isinstance(payload, Mapping):
        raise CatalogManifestValidationError("Catalog field 'limits' must be an object")
    context_window_tokens = payload.get("context_window_tokens")
    max_output_tokens = payload.get("max_output_tokens")
    if not isinstance(context_window_tokens, int) or context_window_tokens <= 0:
        raise CatalogManifestValidationError("context_window_tokens must be positive")
    if not isinstance(max_output_tokens, int) or max_output_tokens <= 0:
        raise CatalogManifestValidationError("max_output_tokens must be positive")
    return context_window_tokens, max_output_tokens


def _require_non_empty_sequence(payload: Mapping[str, Any], key: str) -> tuple[Any, ...]:
    """Return a required non-empty list/tuple field."""
    values = _optional_sequence(payload, key)
    if not values:
        raise CatalogManifestValidationError(f"Catalog field '{key}' must be non-empty")
    return values


def _optional_sequence(payload: Mapping[str, Any], key: str) -> tuple[Any, ...]:
    """Return an optional sequence field as a tuple."""
    values = payload.get(key, ())
    if not isinstance(values, (list, tuple)):
        raise CatalogManifestValidationError(f"Catalog field '{key}' must be a list")
    return tuple(values)


def _string_tuple(values: Any, key: str) -> tuple[str, ...]:
    """Return a tuple of non-empty strings."""
    if not isinstance(values, (list, tuple)):
        raise CatalogManifestValidationError(f"Catalog field '{key}' must be a list")
    result = tuple(str(value).strip() for value in values if str(value).strip())
    if len(result) != len(values):
        raise CatalogManifestValidationError(
            f"Catalog field '{key}' cannot contain empty values"
        )
    return result


__all__ = [
    "CatalogManifest",
    "CatalogManifestValidationError",
    "CatalogModel",
    "build_model_profiles_from_manifest",
    "load_catalog_manifest",
]
