"""Contract tests for deterministic isolated task runtime network allocation."""

from ipaddress import ip_network
from itertools import islice

import pytest

from runtime_shared.runtime_network import (
    RuntimeNetworkError,
    build_runtime_network_name,
    build_runtime_network_spec,
    iter_runtime_subnets,
    network_labels,
    parse_runtime_network_pool,
    validate_managed_network,
)


def _spec(identity: str = "drowai-tenant-a-task-7"):
    return build_runtime_network_spec(
        container_name="drowai-tenant-a-task-7",
        runtime_identity=identity,
        tenant_id="tenant-a",
        task_id=7,
        runtime_owner="runner",
        pool=parse_runtime_network_pool(None),
    )


def test_default_pool_selects_stable_non_overlapping_29() -> None:
    spec = _spec()
    first = next(iter_runtime_subnets(spec))
    assert first == next(iter_runtime_subnets(spec))
    assert first.prefixlen == 29
    second = next(iter_runtime_subnets(spec, [first]))
    assert second != first
    assert not second.overlaps(first)


def test_network_name_preserves_the_complete_container_identity() -> None:
    container_name = f"drowai-{'tenant-segment-' * 6}task-7"

    assert build_runtime_network_name(container_name) == f"{container_name}-net"


@pytest.mark.parametrize(
    "value", ["8.8.8.0/24", "198.18.0.1/15", "198.18.0.0/30", "fd00::/64"]
)
def test_pool_validation_fails_closed(value: str) -> None:
    with pytest.raises(RuntimeNetworkError):
        parse_runtime_network_pool(value)


def test_existing_network_requires_exact_ownership_ipam_and_bridge_options() -> None:
    spec = _spec()
    subnet = next(iter_runtime_subnets(spec))
    attributes = {
        "Driver": "bridge",
        "Internal": False,
        "Labels": network_labels(spec, subnet),
        "Options": {
            "com.docker.network.bridge.enable_ip_masquerade": "true",
            "com.docker.network.bridge.enable_icc": "false",
        },
        "IPAM": {"Config": [{"Subnet": str(subnet)}]},
    }
    assert validate_managed_network(spec, attributes) == subnet

    attributes["Labels"] = {**attributes["Labels"], "drowai.tenant_id": "other"}
    with pytest.raises(RuntimeNetworkError, match="ownership"):
        validate_managed_network(spec, attributes)


def test_pool_exhaustion_yields_no_candidate() -> None:
    spec = build_runtime_network_spec(
        container_name="task-1",
        runtime_identity="task-1",
        tenant_id="t",
        task_id=1,
        runtime_owner="runner",
        pool=parse_runtime_network_pool("198.18.0.0/29"),
    )
    assert list(iter_runtime_subnets(spec, [ip_network("198.18.0.0/29")])) == []


def test_large_operator_pool_is_generated_lazily() -> None:
    spec = build_runtime_network_spec(
        container_name="task-large",
        runtime_identity="task-large",
        tenant_id="t",
        task_id=2,
        runtime_owner="runner",
        pool=parse_runtime_network_pool("10.0.0.0/8"),
    )
    candidates = list(islice(iter_runtime_subnets(spec), 2))
    assert len(candidates) == 2
    assert all(candidate.prefixlen == 29 for candidate in candidates)
