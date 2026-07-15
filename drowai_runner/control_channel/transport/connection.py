"""Websocket connect for the runner cloud control channel.

Opens an outbound websocket to the control channel endpoint with runner auth
headers and TLS when required. Yields a locked websocket wrapper for session use.
Does not import ``drowai_runner.cloud_client``; used by ``ConnectedSessionPump``.
"""

from __future__ import annotations

from contextlib import contextmanager
import logging
from urllib import parse as urllib_parse

from websockets.sync.client import connect as ws_connect

from drowai_runner.config import RunnerConfig
from drowai_runner.credentials import mask_secret
from drowai_runner.control_channel.constants import DEFAULT_OPEN_TIMEOUT_SECONDS
from drowai_runner.control_channel.identity.models import CloudChannelIdentity
from drowai_runner.control_channel.transport.endpoint import (
    _build_ssl_context,
    _to_websocket_url,
)
from drowai_runner.control_channel.transport.locked_websocket import _LockedWebSocket

logger = logging.getLogger(__name__)


class CloudChannelConnector:
    """Opens authenticated websocket connections to the cloud control channel."""

    def __init__(self, *, config: RunnerConfig) -> None:
        self._config = config

    @contextmanager
    def connect(self, identity: CloudChannelIdentity):
        ws_url = _to_websocket_url(identity.channel_endpoint)
        headers = {
            "x-runner-tenant-id": str(identity.tenant_id),
            "x-runner-id": identity.runner_id,
            "x-runner-credential-secret": identity.credential_secret,
        }
        connect_kwargs = {
            "additional_headers": headers,
            "open_timeout": DEFAULT_OPEN_TIMEOUT_SECONDS,
            "ping_interval": None,
            "ping_timeout": None,
        }
        if urllib_parse.urlparse(ws_url).scheme == "wss":
            connect_kwargs["ssl"] = _build_ssl_context(verify=self._config.tls_verify)
        with ws_connect(ws_url, **connect_kwargs) as raw_websocket:
            websocket = _LockedWebSocket(raw_websocket)
            logger.info(
                "runner.cloud.channel_connected tenant_id=%s runner_id=%s credential_secret=%s",
                identity.tenant_id,
                identity.runner_id,
                mask_secret(identity.credential_secret),
            )
            yield websocket
