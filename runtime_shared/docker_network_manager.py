"""Backend-free Docker SDK adapter for the shared task-network contract.

Both local Docker and managed Runner providers delegate network creation,
collision recovery, validation, and safe empty-network removal to this adapter.
"""

from __future__ import annotations

from dataclasses import dataclass
from ipaddress import IPv4Network, ip_network
from typing import Any, Callable, Mapping

from runtime_shared.runtime_network import (
    RUNTIME_NETWORK_DRIVER,
    RUNTIME_NETWORK_OPTIONS,
    RuntimeNetworkError,
    RuntimeNetworkSpec,
    SUBNET_LABEL,
    TASK_LABEL,
    TENANT_LABEL,
    is_owned_task_network,
    iter_runtime_subnets,
    network_labels,
    validate_managed_network,
)


@dataclass(frozen=True, slots=True)
class NetworkProvisionResult:
    """Safe diagnostic result for a created or reused task bridge."""

    name: str
    subnet: str
    created: bool


@dataclass(frozen=True, slots=True)
class DockerTaskNetworkManager:
    """Perform Docker SDK operations while enforcing the pure shared contract."""

    client_factory: Callable[[], Any]

    def ensure(self, spec: RuntimeNetworkSpec) -> NetworkProvisionResult:
        client = self.client_factory()
        existing = self._by_name(client, spec.name)
        if existing is not None:
            subnet = validate_managed_network(spec, _attributes(existing))
            return NetworkProvisionResult(spec.name, str(subnet), False)

        attempted: set[IPv4Network] = set()
        while True:
            candidate = next(
                (
                    item
                    for item in iter_runtime_subnets(spec, _occupied_subnets(client))
                    if item not in attempted
                ),
                None,
            )
            if candidate is None:
                raise RuntimeNetworkError("Managed runtime network pool is exhausted.")
            attempted.add(candidate)
            try:
                _create(client, spec, candidate)
            except Exception as exc:
                raced = self._by_name(client, spec.name)
                if raced is not None:
                    subnet = validate_managed_network(spec, _attributes(raced))
                    return NetworkProvisionResult(spec.name, str(subnet), False)
                if "overlap" in str(exc).lower():
                    continue
                raise
            return NetworkProvisionResult(spec.name, str(candidate), True)

    def remove_empty(self, spec: RuntimeNetworkSpec) -> bool:
        """Remove only an empty, exactly owned bridge, independent of current pool."""
        client = self.client_factory()
        network = self._by_name(client, spec.name)
        if network is None:
            return False
        attributes = _attributes(network)
        validate_managed_network(spec, attributes, require_current_pool=False)
        if attributes.get("Containers") or attributes.get("containers"):
            return False
        network.remove()
        return True

    def remove_empty_owned(self, *, name: str, runtime_owner: str) -> bool:
        """Remove an empty orphan bridge only when its full ownership contract is valid."""
        client = self.client_factory()
        network = self._by_name(client, name)
        if network is None:
            return False
        attributes = _attributes(network)
        labels = attributes.get("Labels") or {}
        if not isinstance(labels, Mapping) or not is_owned_task_network(
            attributes, runtime_owner=runtime_owner
        ):
            raise RuntimeNetworkError(f"Network {name} is not owned by {runtime_owner}.")
        tenant_id = str(labels.get(TENANT_LABEL) or "")
        task_id = str(labels.get(TASK_LABEL) or "")
        subnet = str(labels.get(SUBNET_LABEL) or "")
        if not tenant_id or not task_id or not subnet:
            raise RuntimeNetworkError(f"Network {name} has incomplete ownership labels.")
        spec = RuntimeNetworkSpec(
            name=name,
            runtime_identity=name,
            tenant_id=tenant_id,
            task_id=task_id,
            runtime_owner=runtime_owner,
            pool=ip_network(subnet, strict=True),
        )
        validate_managed_network(spec, attributes, require_current_pool=False)
        if attributes.get("Containers") or attributes.get("containers"):
            return False
        network.remove()
        return True

    @staticmethod
    def _by_name(client: Any, name: str) -> Any | None:
        try:
            return client.networks.get(name)
        except Exception as exc:
            if isinstance(exc, KeyError) or "not found" in str(exc).lower():
                return None
            raise


def _attributes(network: Any) -> Mapping[str, object]:
    reload_network = getattr(network, "reload", None)
    if callable(reload_network):
        reload_network()
    attributes = getattr(network, "attrs", None)
    if not isinstance(attributes, Mapping):
        raise RuntimeNetworkError("Docker network inspection returned invalid attributes.")
    return attributes


def _occupied_subnets(client: Any) -> tuple[IPv4Network, ...]:
    occupied: list[IPv4Network] = []
    for network in client.networks.list():
        try:
            attributes = _attributes(network)
        except RuntimeNetworkError:
            continue
        ipam = attributes.get("IPAM") or {}
        configs = ipam.get("Config", []) if isinstance(ipam, Mapping) else []
        for config in configs if isinstance(configs, list) else []:
            if not isinstance(config, Mapping) or not config.get("Subnet"):
                continue
            try:
                subnet = ip_network(str(config["Subnet"]), strict=False)
            except ValueError:
                continue
            if isinstance(subnet, IPv4Network):
                occupied.append(subnet)
    return tuple(occupied)


def _create(client: Any, spec: RuntimeNetworkSpec, subnet: IPv4Network) -> Any:
    from docker.types import IPAMConfig, IPAMPool

    return client.networks.create(
        spec.name,
        driver=RUNTIME_NETWORK_DRIVER,
        internal=False,
        check_duplicate=True,
        options=dict(RUNTIME_NETWORK_OPTIONS),
        labels=network_labels(spec, subnet),
        ipam=IPAMConfig(pool_configs=[IPAMPool(subnet=str(subnet))]),
    )
