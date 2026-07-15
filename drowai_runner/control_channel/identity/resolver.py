"""Cloud channel identity resolution for runner cloud mode.

Loads persisted runner credentials or orchestrates registration, persistence, and
identity assembly. Does not import ``drowai_runner.cloud_client``; wired by the client.
"""

from __future__ import annotations

import logging

from drowai_runner.config import RunnerConfig
from drowai_runner.credentials import mask_secret
from runtime_shared.runner_protocol import (
    RUNNER_PROTOCOL_SCHEMA_VERSION,
    RUNNER_PROTOCOL_DATA_PLANE_VERSION,
)

from drowai_runner.control_channel.errors import RunnerCloudClientError
from drowai_runner.control_channel.helpers import _stream_capabilities
from drowai_runner.control_channel.identity.environment import _default_runner_name
from drowai_runner.control_channel.identity.models import (
    CloudChannelIdentity,
    RegistrationRequest,
)
from drowai_runner.control_channel.identity.persistence import (
    _load_runner_id_if_present,
    _load_runner_protocol_version_if_present,
    _load_runner_secret_if_present,
    _load_runner_tenant_id_if_present,
    _persist_runner_id,
    _persist_runner_protocol_version,
    _persist_runner_secret,
    _persist_runner_tenant_id,
)
from drowai_runner.control_channel.identity.registration import RunnerRegistrationClient

logger = logging.getLogger(__name__)


class CloudChannelIdentityResolver:
    """Resolves runner cloud channel identity from storage or registration."""

    def __init__(
        self,
        *,
        config: RunnerConfig,
        tenant_id: int | None,
        runner_version: str,
        channel_endpoint: str,
        registration_client: RunnerRegistrationClient,
    ) -> None:
        self._config = config
        self._tenant_id = tenant_id
        self._runner_version = runner_version
        self._channel_endpoint = channel_endpoint
        self._registration_client = registration_client

    def resolve(self) -> CloudChannelIdentity:
        stored_secret = _load_runner_secret_if_present(self._config)
        configured_runner_id = (self._config.runner_id or "").strip()
        stored_runner_id = _load_runner_id_if_present(self._config)
        runner_id = configured_runner_id or stored_runner_id
        tenant_id = self._tenant_id or _load_runner_tenant_id_if_present(self._config)

        if stored_secret and runner_id and tenant_id:
            return CloudChannelIdentity(
                tenant_id=tenant_id,
                runner_id=runner_id,
                credential_secret=stored_secret,
                channel_endpoint=self._channel_endpoint,
                protocol_version=(
                    _load_runner_protocol_version_if_present(self._config)
                    or RUNNER_PROTOCOL_DATA_PLANE_VERSION
                ),
                heartbeat_interval_seconds=self._config.heartbeat_interval_seconds,
            )

        registration_token = (self._config.registration_token or "").strip()
        if not registration_token:
            raise RunnerCloudClientError(
                error_code="RUNNER_CLOUD_IDENTITY_MISSING",
                message="cloud mode requires stored runner credentials (runner_id + credential secret), or a registration token.",
            )

        request_payload = RegistrationRequest(
            install_token=registration_token,
            runner_name=_default_runner_name(),
            runner_version=self._runner_version,
            labels=dict(self._config.labels or {}),
            capabilities=list(_stream_capabilities(self._config.capabilities)),
            tenant_id=self._tenant_id,
        )
        result = self._registration_client.register(request_payload)
        _persist_runner_secret(self._config, result.credential_secret)
        _persist_runner_id(self._config, result.runner_id)
        _persist_runner_tenant_id(self._config, result.tenant_id)
        _persist_runner_protocol_version(self._config, result.protocol_version)
        logger.info(
            "runner.cloud.registration_succeeded tenant_id=%s runner_id=%s registration_token=%s credential_secret=%s",
            result.tenant_id,
            result.runner_id,
            mask_secret(self._config.registration_token),
            mask_secret(result.credential_secret),
        )
        return CloudChannelIdentity(
            tenant_id=result.tenant_id,
            runner_id=result.runner_id,
            credential_secret=result.credential_secret,
            channel_endpoint=(result.channel_endpoint or self._channel_endpoint),
            protocol_version=(result.protocol_version or RUNNER_PROTOCOL_SCHEMA_VERSION),
            heartbeat_interval_seconds=max(1, int(result.heartbeat_interval_seconds)),
        )
