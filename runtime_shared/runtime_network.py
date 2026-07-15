"""Pure contract for isolated, runner-agnostic task runtime networks.

This module validates the operator address pool and deterministically describes
per-task Docker bridge names, ownership labels, and collision-safe /29 choices.
It intentionally contains no backend or Docker SDK dependencies.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from ipaddress import IPv4Address, IPv4Network, ip_network
import re
from typing import Iterable, Iterator, Mapping

RUNTIME_NETWORK_POOL_ENV = "DROWAI_RUNTIME_NETWORK_POOL"
DEFAULT_RUNTIME_NETWORK_POOL = "198.18.0.0/15"
RUNTIME_NETWORK_PREFIX = 29
RUNTIME_NETWORK_DRIVER = "bridge"
RUNTIME_NETWORK_OPTIONS = {
    "com.docker.network.bridge.enable_ip_masquerade": "true",
    "com.docker.network.bridge.enable_icc": "false",
}
MANAGED_RESOURCE_LABEL = "drowai.managed_resource"
MANAGED_RESOURCE_VALUE = "task-network"
RUNTIME_OWNER_LABEL = "drowai.runtime_owner"
TENANT_LABEL = "drowai.tenant_id"
TASK_LABEL = "drowai.task_id"
SUBNET_LABEL = "drowai.network_subnet"
_NAME_UNSAFE = re.compile(r"[^a-z0-9_.-]+")


class RuntimeNetworkError(ValueError):
    """Raised when the managed-network contract cannot be satisfied safely."""


@dataclass(frozen=True, slots=True)
class RuntimeNetworkSpec:
    """Stable identity and ownership attributes for one task bridge."""

    name: str
    runtime_identity: str
    tenant_id: str
    task_id: str
    runtime_owner: str
    pool: IPv4Network

    @property
    def labels(self) -> dict[str, str]:
        return {
            MANAGED_RESOURCE_LABEL: MANAGED_RESOURCE_VALUE,
            RUNTIME_OWNER_LABEL: self.runtime_owner,
            TENANT_LABEL: self.tenant_id,
            TASK_LABEL: self.task_id,
        }


def parse_runtime_network_pool(raw: str | None) -> IPv4Network:
    """Validate the optional operator pool as non-global IPv4 with /29 capacity."""
    value = (raw or DEFAULT_RUNTIME_NETWORK_POOL).strip()
    try:
        pool = ip_network(value, strict=True)
    except ValueError as exc:
        raise RuntimeNetworkError(
            f"{RUNTIME_NETWORK_POOL_ENV} must be a canonical IPv4 CIDR."
        ) from exc
    if not isinstance(pool, IPv4Network):
        raise RuntimeNetworkError(f"{RUNTIME_NETWORK_POOL_ENV} must be IPv4.")
    if pool.is_global:
        raise RuntimeNetworkError(
            f"{RUNTIME_NETWORK_POOL_ENV} must use non-global address space."
        )
    if pool.prefixlen > RUNTIME_NETWORK_PREFIX:
        raise RuntimeNetworkError(
            f"{RUNTIME_NETWORK_POOL_ENV} must contain at least one /{RUNTIME_NETWORK_PREFIX}."
        )
    return pool


def build_runtime_network_name(container_name: str) -> str:
    """Return a Docker-safe network name derived from the existing container name."""
    normalized = _NAME_UNSAFE.sub("-", container_name.strip().lower()).strip("-.")
    if not normalized:
        raise RuntimeNetworkError(
            "Runtime container name cannot produce a network name."
        )
    return f"{normalized}-net"


def build_runtime_network_spec(
    *,
    container_name: str,
    runtime_identity: str,
    tenant_id: str | int,
    task_id: str | int,
    runtime_owner: str,
    pool: IPv4Network,
) -> RuntimeNetworkSpec:
    """Build the shared network identity consumed by local and runner providers."""
    return RuntimeNetworkSpec(
        name=build_runtime_network_name(container_name),
        runtime_identity=str(runtime_identity),
        tenant_id=str(tenant_id),
        task_id=str(task_id),
        runtime_owner=str(runtime_owner),
        pool=pool,
    )


def iter_runtime_subnets(
    spec: RuntimeNetworkSpec,
    occupied: Iterable[IPv4Network | str] = (),
) -> Iterator[IPv4Network]:
    """Yield deterministic /29 candidates, skipping every overlapping Docker subnet."""
    candidate_count = 1 << (RUNTIME_NETWORK_PREFIX - spec.pool.prefixlen)
    digest = hashlib.sha256(spec.runtime_identity.encode("utf-8")).digest()
    start = int.from_bytes(digest[:8], "big") % candidate_count
    parsed_occupied = tuple(_coerce_ipv4_network(item) for item in occupied)
    subnet_size = 1 << (32 - RUNTIME_NETWORK_PREFIX)
    base_address = int(spec.pool.network_address)
    for offset in range(candidate_count):
        index = (start + offset) % candidate_count
        candidate = IPv4Network(
            (IPv4Address(base_address + index * subnet_size), RUNTIME_NETWORK_PREFIX)
        )
        if any(candidate.overlaps(existing) for existing in parsed_occupied):
            continue
        yield candidate


def network_labels(spec: RuntimeNetworkSpec, subnet: IPv4Network) -> dict[str, str]:
    """Return immutable ownership labels including the allocated subnet."""
    return {**spec.labels, SUBNET_LABEL: str(subnet)}


def validate_managed_network(
    spec: RuntimeNetworkSpec,
    attributes: Mapping[str, object],
    *,
    require_current_pool: bool = True,
) -> IPv4Network:
    """Validate that an existing same-name Docker network is safe to reuse."""
    driver = str(attributes.get("Driver") or attributes.get("driver") or "")
    internal = bool(attributes.get("Internal", attributes.get("internal", False)))
    labels = attributes.get("Labels") or attributes.get("labels") or {}
    options = attributes.get("Options") or attributes.get("options") or {}
    if driver != RUNTIME_NETWORK_DRIVER or internal:
        raise RuntimeNetworkError(
            f"Network {spec.name} has incompatible bridge settings."
        )
    if not isinstance(labels, Mapping) or any(
        labels.get(k) != v for k, v in spec.labels.items()
    ):
        raise RuntimeNetworkError(
            f"Network {spec.name} is foreign or has invalid ownership labels."
        )
    if not isinstance(options, Mapping) or any(
        options.get(k) != v for k, v in RUNTIME_NETWORK_OPTIONS.items()
    ):
        raise RuntimeNetworkError(
            f"Network {spec.name} has incompatible external-access settings."
        )
    subnet_text = str(labels.get(SUBNET_LABEL) or "")
    subnet = _coerce_ipv4_network(subnet_text)
    if subnet.prefixlen != RUNTIME_NETWORK_PREFIX or (
        require_current_pool and not subnet.subnet_of(spec.pool)
    ):
        raise RuntimeNetworkError(
            f"Network {spec.name} uses a subnet outside the managed pool."
        )
    inspected_subnets = _inspection_subnets(attributes)
    if inspected_subnets != (subnet,):
        raise RuntimeNetworkError(
            f"Network {spec.name} has incompatible IPAM configuration."
        )
    return subnet


def is_owned_task_network(
    attributes: Mapping[str, object], *, runtime_owner: str
) -> bool:
    """Return whether inspection data identifies a network owned by this provider."""
    labels = attributes.get("Labels") or attributes.get("labels") or {}
    return bool(
        isinstance(labels, Mapping)
        and labels.get(MANAGED_RESOURCE_LABEL) == MANAGED_RESOURCE_VALUE
        and labels.get(RUNTIME_OWNER_LABEL) == runtime_owner
    )


def _inspection_subnets(attributes: Mapping[str, object]) -> tuple[IPv4Network, ...]:
    ipam = attributes.get("IPAM") or attributes.get("ipam") or {}
    configs = ipam.get("Config", []) if isinstance(ipam, Mapping) else []
    subnets: list[IPv4Network] = []
    for config in configs if isinstance(configs, list) else []:
        if isinstance(config, Mapping) and config.get("Subnet"):
            subnets.append(_coerce_ipv4_network(str(config["Subnet"])))
    return tuple(subnets)


def _coerce_ipv4_network(value: IPv4Network | str) -> IPv4Network:
    if isinstance(value, IPv4Network):
        return value
    try:
        parsed = ip_network(str(value), strict=False)
    except ValueError as exc:
        raise RuntimeNetworkError(f"Invalid Docker network subnet: {value!s}") from exc
    if not isinstance(parsed, IPv4Network):
        raise RuntimeNetworkError("Managed task networks require IPv4 subnets.")
    return parsed
