"""Host-visible workspace bind path resolution for containerized runners.

When a runner process uses the host Docker socket, Kali bind mounts must refer
to paths visible on the host filesystem, not only inside the runner container.
"""

from __future__ import annotations

from pathlib import Path


def resolve_workspace_bind_source(
    workspace_path: Path,
    *,
    runner_root: Path,
    host_bind_root: Path | None,
) -> str:
    """Map a runner-local workspace path to a host-visible Docker bind source."""
    resolved_workspace = workspace_path.expanduser().resolve()
    resolved_runner_root = runner_root.expanduser().resolve()
    if host_bind_root is None:
        return str(resolved_workspace)

    resolved_host_root = host_bind_root.expanduser().resolve()
    try:
        relative = resolved_workspace.relative_to(resolved_runner_root)
    except ValueError as exc:
        raise ValueError(
            "workspace_path must be under runner_root when host_bind_root is set."
        ) from exc
    return str(resolved_host_root / relative)
