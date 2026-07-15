"""Cloud mode entrypoint for the runner control channel.

Provides ``run_cloud_mode`` and the Docker client factory used by composition wiring.
``run_cloud_mode`` lazy-imports ``RunnerCloudClient`` to avoid a module-level import
cycle with ``drowai_runner.cloud_client``.
"""

from __future__ import annotations

import logging

from drowai_runner.config import RunnerConfig

logger = logging.getLogger(__name__)


def _docker_client_factory() -> object:
    import docker

    return docker.from_env()


def run_cloud_mode(config: RunnerConfig) -> int:
    """Run cloud mode process and map interrupts to a clean exit code."""
    from drowai_runner.cloud_client import RunnerCloudClient

    client = RunnerCloudClient(config=config)
    try:
        client.run_forever()
    except KeyboardInterrupt:
        logger.info("runner.cloud.shutdown_requested")
    return 0
