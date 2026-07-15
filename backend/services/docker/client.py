"""
Docker client/bootstrap module for unified Docker decomposition.

Scope:
- Initialize Docker access mode (SDK, CLI fallback, or simulation).
- Own low-level client state shared by higher-level Docker modules.

Boundary:
- Contains no container lifecycle orchestration logic.
- Does not depend on other decomposed Docker modules.
"""

import logging
import os
import subprocess

import docker

from runtime_shared.runtime_image_contract import default_runtime_image_for_machine
from runtime_shared.runtime_network import parse_runtime_network_pool

logger = logging.getLogger(__name__)


def check_docker_cli_availability() -> bool:
    """Check if Docker CLI is available as fallback."""
    try:
        result = subprocess.run(
            ["docker", "--version"], capture_output=True, text=True, timeout=5
        )
        return result.returncode == 0
    except Exception:
        return False


class DockerClient:
    """Low-level Docker connectivity and mode selection."""

    def __init__(self, docker_factory=None, cli_checker=None):
        self.runtime_network_pool = parse_runtime_network_pool(
            os.getenv("DROWAI_RUNTIME_NETWORK_POOL")
        )
        # Default runtime uses image packaging under /opt/drowai/runtime assets.
        # Explicit env override remains available for rollback/testing environments.
        self.image_name = os.getenv(
            "DROWAI_RUNTIME_IMAGE",
            os.getenv("CONTAINER_IMAGE", default_runtime_image_for_machine()),
        )
        self.containers = {}
        if docker_factory is None:
            docker_factory = docker.from_env
        if cli_checker is None:
            cli_checker = check_docker_cli_availability

        # Initialize Docker client with fallback to CLI
        try:
            self.client = docker_factory()
            self.client.ping()
            self.docker_available = True
            self.api_mode = "sdk"
            logger.info("Docker SDK initialized successfully")
        except Exception as e:
            logger.warning(f"Docker SDK unavailable: {e}")
            self.client = None
            self.docker_available = cli_checker()
            self.api_mode = "cli" if self.docker_available else "simulation"

    def _check_docker_cli_availability(self) -> bool:
        """Backward-compatible wrapper to module-level availability helper."""
        return check_docker_cli_availability()

    def is_docker_available(self) -> bool:
        """Check if Docker is available."""
        return self.docker_available
