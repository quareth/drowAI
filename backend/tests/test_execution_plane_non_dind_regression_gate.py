"""Execution-plane non-DinD gate contract (documentation only).

This module does not define tests. Selected suites are tagged with
``pytest.mark.execution_plane_non_dind_regression`` so CI and reviewers can
run a single, Docker-in-Docker-free command:

    pytest -m execution_plane_non_dind_regression backend/tests

Contract alignment:

- This marker represents the non-DinD production-target gate: writable workspace
  plus read-only control mounts and image-internal startup/VPN runtime paths.
- Rollback is release/image rollback; this marker is not a runtime-path mode
  rollback selector.

Coverage mapping (implementation guide Task 4.1 gate set):

- **Startup / task state transitions**: ``test_task_status.py`` (domain transitions);
  ``test_task_services_refactor.py`` (lifecycle service create/delegate);
  ``test_container_lifecycle.py`` (simulated container controls).
- **Workspace / file-comm contract**: ``test_file_comm.py`` (JSONL round-trip).
- **VPN behavior (path-source parity, API shape)**: ``test_vpn_api.py``;
  ``test_runtime_validation_mode_config.py`` (startup chain, VPN resolver, diagnostics).
- **Workspace/control mount-policy enforcement**: ``test_runner_control_mount_policy_resolver.py``
  (non-DinD two-bind contract + startup/VPN image-internal enforcement checks).

Requires no live task container; uses mocked Docker and sqlite test DB from ``conftest.py``.
"""

__all__: list[str] = []
