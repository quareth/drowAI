"""HTTP registration client for runner cloud control-channel identity bootstrap.

Posts install-token registration to the control plane and parses the credential
response. Does not import ``drowai_runner.cloud_client``; used by identity resolver.
"""

from __future__ import annotations

import json
from urllib import error as urllib_error
from urllib import request as urllib_request

from runtime_shared.runner_protocol import RUNNER_PROTOCOL_SCHEMA_VERSION

from drowai_runner.control_channel.constants import DEFAULT_CONNECT_TIMEOUT_SECONDS
from drowai_runner.control_channel.errors import RunnerCloudClientError
from drowai_runner.control_channel.identity.models import (
    RegistrationRequest,
    RegistrationResult,
)
from drowai_runner.control_channel.transport.endpoint import _build_ssl_context


class RunnerRegistrationClient:
    """Registers a runner with the cloud control plane and returns credentials."""

    def __init__(self, *, registration_url: str, tls_verify: bool) -> None:
        self._registration_url = registration_url
        self._tls_verify = tls_verify

    def register(self, payload: RegistrationRequest) -> RegistrationResult:
        request_body = json.dumps(
            {
                "install_token": payload.install_token,
                "runner_name": payload.runner_name,
                "runner_version": payload.runner_version,
                "labels": payload.labels,
                "capabilities": payload.capabilities,
                **({"tenant_id": payload.tenant_id} if payload.tenant_id is not None else {}),
            }
        ).encode("utf-8")
        request = urllib_request.Request(
            self._registration_url,
            data=request_body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        context = _build_ssl_context(verify=self._tls_verify)
        try:
            with urllib_request.urlopen(
                request,
                timeout=DEFAULT_CONNECT_TIMEOUT_SECONDS,
                context=context,
            ) as response:
                body = response.read().decode("utf-8")
        except urllib_error.HTTPError as exc:
            raise RunnerCloudClientError(
                error_code="RUNNER_REGISTRATION_FAILED",
                message=f"runner registration failed with status {exc.code}.",
            ) from exc
        except urllib_error.URLError as exc:
            raise RunnerCloudClientError(
                error_code="RUNNER_REGISTRATION_UNREACHABLE",
                message=f"runner registration failed: {exc.reason}",
            ) from exc

        try:
            parsed = json.loads(body)
        except json.JSONDecodeError as exc:
            raise RunnerCloudClientError(
                error_code="RUNNER_REGISTRATION_INVALID_RESPONSE",
                message="runner registration response is not valid JSON.",
            ) from exc

        runner_id = str(parsed.get("runner_id") or "").strip()
        tenant_id = int(parsed.get("tenant_id") or 0)
        credential_secret = str(parsed.get("credential_secret") or "").strip()
        channel_endpoint = str(parsed.get("channel_endpoint") or "").strip()
        protocol_version = str(parsed.get("protocol_version") or "").strip()
        heartbeat_interval_seconds = int(parsed.get("heartbeat_interval_seconds") or 30)

        if not runner_id or tenant_id < 1 or not credential_secret:
            raise RunnerCloudClientError(
                error_code="RUNNER_REGISTRATION_INVALID_RESPONSE",
                message="runner registration response missing runner_id, tenant_id, or credential_secret.",
            )
        return RegistrationResult(
            runner_id=runner_id,
            tenant_id=tenant_id,
            credential_secret=credential_secret,
            channel_endpoint=channel_endpoint,
            protocol_version=protocol_version or RUNNER_PROTOCOL_SCHEMA_VERSION,
            heartbeat_interval_seconds=max(1, heartbeat_interval_seconds),
        )
