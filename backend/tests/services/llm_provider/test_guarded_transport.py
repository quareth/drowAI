"""Tests for bounded, proxy-free, redirect-free guarded LLM transport."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest
import requests

import backend.services.llm_provider.guarded_transport as guarded_transport_module
from backend.services.llm_provider.egress_policy import FixedProviderEgressPolicy
from backend.services.llm_provider.guarded_transport import (
    GuardedAsyncInferenceTransport,
    GuardedTransport,
    GuardedTransportError,
)
from backend.services.llm_provider.operation_registry import (
    CUSTOM_OPENAI_COMPATIBLE_PRESET_ID,
    ConnectionOperationRegistry,
    GPT_OSS_20B_PROVING_BASE_URL_ENV,
    GPT_OSS_20B_PROVING_PRESET_ID,
    HUGGINGFACE_OPENAI_COMPATIBLE_PRESET_ID,
    NVIDIA_NIM_BASE_URL_ENV,
    NVIDIA_NIM_OPENAI_COMPATIBLE_PRESET_ID,
    OLLAMA_OPENAI_COMPATIBLE_PRESET_ID,
    VLLM_OPENAI_COMPATIBLE_PRESET_ID,
)
from backend.services.llm_provider.types import (
    GuardedEgressBounds,
    GuardedEgressTimeouts,
    LLMConnectionOperation,
    ProviderSecret,
)


class _Response:
    """Minimal bounded streaming response used by guarded transport tests."""

    def __init__(
        self,
        *,
        status_code: int = 200,
        body: bytes = b'{"data":[]}',
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status_code = status_code
        self._body = body
        self.headers = (
            {"content-length": str(len(body))} if headers is None else headers
        )
        self.closed = False

    def iter_content(self, chunk_size: int) -> Any:
        for index in range(0, len(self._body), chunk_size):
            yield self._body[index : index + chunk_size]

    def close(self) -> None:
        self.closed = True


class _Session:
    """Requests-like session recording security-critical request arguments."""

    def __init__(self, response: _Response | None = None) -> None:
        self.trust_env = True
        self.response = response or _Response()
        self.calls: list[dict[str, Any]] = []
        self.closed = False
        self.error: Exception | None = None
        self.close_error: Exception | None = None

    def request(self, method: str, url: str, **kwargs: Any) -> _Response:
        self.calls.append({"method": method, "url": url, **kwargs})
        if self.error is not None:
            raise self.error
        return self.response

    def close(self) -> None:
        self.closed = True
        if self.close_error is not None:
            raise self.close_error


def _transport(
    session: _Session,
    *,
    dns_answers: list[tuple[str, ...]] | None = None,
    bounds: GuardedEgressBounds | None = None,
) -> GuardedTransport:
    """Build guarded transport with deterministic network dependencies."""

    answers = iter(dns_answers or [("93.184.216.34",), ("93.184.216.34",)])
    policy = FixedProviderEgressPolicy(
        dns_resolver=lambda _host, _port: next(answers),
    )
    return GuardedTransport(
        registry=ConnectionOperationRegistry(),
        egress_policy=policy,
        session_factory=lambda: session,
        timeouts=GuardedEgressTimeouts(
            connect_seconds=1.0,
            read_seconds=2.0,
            total_seconds=3.0,
        ),
        bounds=bounds or GuardedEgressBounds(),
    )


def _async_transport(
    handler: Any,
    *,
    bounds: GuardedEgressBounds | None = None,
) -> GuardedAsyncInferenceTransport:
    """Build async inference transport with deterministic DNS and HTTP I/O."""

    policy = FixedProviderEgressPolicy(
        dns_resolver=lambda _host, _port: ("93.184.216.34",),
    )
    target = ConnectionOperationRegistry().resolve(
        LLMConnectionOperation.INFERENCE,
        provider="openai",
    )

    def client_factory(**kwargs: Any) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            **kwargs,
        )

    return GuardedAsyncInferenceTransport(
        operation_target=target,
        secret=ProviderSecret(provider="openai", value="sk-secret"),
        egress_policy=policy,
        client_factory=client_factory,
        timeouts=GuardedEgressTimeouts(
            connect_seconds=1.0,
            read_seconds=1.0,
            total_seconds=2.0,
        ),
        bounds=bounds,
    )


class _DelayedSSEStream(httpx.AsyncByteStream):
    """Yield one SSE event, then wait before yielding the terminal events."""

    def __init__(self, gate: asyncio.Event) -> None:
        self._gate = gate
        self.closed = False

    async def __aiter__(self) -> Any:
        yield b'data: {"id":"one","choices":[{"delta":{"content":"first"}}]}\n\n'
        await self._gate.wait()
        yield b'data: {"id":"two","choices":[{"delta":{"content":"second"}}]}\n\n'
        yield b"data: [DONE]\n\n"

    async def aclose(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_async_inference_request_does_not_block_event_loop() -> None:
    """A slow provider wait leaves graph and stream publisher tasks runnable."""

    gate = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer sk-secret"
        await gate.wait()
        return httpx.Response(200, json={"choices": []})

    transport = _async_transport(handler)
    pending = asyncio.create_task(transport.request_json({"model": "gpt-5.2"}))
    await asyncio.sleep(0)

    assert pending.done() is False
    gate.set()
    assert await pending == {"choices": []}


@pytest.mark.asyncio
async def test_async_inference_stream_yields_first_sse_event_before_eof() -> None:
    """SSE parsing forwards a provider event without buffering later bytes."""

    gate = asyncio.Event()
    provider_stream = _DelayedSSEStream(gate)

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=provider_stream,
        )

    events = _async_transport(handler).stream_json_events(
        {"model": "gpt-5.2", "stream": True}
    )

    first = await asyncio.wait_for(anext(events), timeout=0.1)
    assert first["choices"][0]["delta"]["content"] == "first"
    assert provider_stream.closed is False

    gate.set()
    second = await asyncio.wait_for(anext(events), timeout=0.1)
    assert second["choices"][0]["delta"]["content"] == "second"
    with pytest.raises(StopAsyncIteration):
        await anext(events)
    assert provider_stream.closed is True


@pytest.mark.asyncio
async def test_async_inference_streams_over_real_delayed_http_connection() -> None:
    """A real loopback HTTP stream exposes its first chunk before completion."""

    gate = asyncio.Event()

    async def handle_client(
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        try:
            await reader.readuntil(b"\r\n\r\n")
            writer.write(
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: text/event-stream\r\n"
                b"Transfer-Encoding: chunked\r\n"
                b"Connection: close\r\n\r\n"
            )
            first = b'data: {"id":"one","choices":[{"delta":{"content":"first"}}]}\n\n'
            writer.write(f"{len(first):x}\r\n".encode() + first + b"\r\n")
            await writer.drain()
            await gate.wait()
            final = b"data: [DONE]\n\n"
            writer.write(f"{len(final):x}\r\n".encode() + final + b"\r\n0\r\n\r\n")
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(handle_client, "127.0.0.1", 0)
    port = int(server.sockets[0].getsockname()[1])
    registry = ConnectionOperationRegistry(
        env_getter={NVIDIA_NIM_BASE_URL_ENV: f"http://127.0.0.1:{port}"}.get
    )
    target = registry.resolve(
        LLMConnectionOperation.INFERENCE,
        provider=NVIDIA_NIM_OPENAI_COMPATIBLE_PRESET_ID,
    )
    transport = GuardedAsyncInferenceTransport(
        operation_target=target,
        secret=ProviderSecret(
            provider=NVIDIA_NIM_OPENAI_COMPATIBLE_PRESET_ID,
            value="sk-local-test",
        ),
    )
    events = transport.stream_json_events({"model": "test", "stream": True})

    try:
        first_event = await asyncio.wait_for(anext(events), timeout=1.0)
        assert first_event["choices"][0]["delta"]["content"] == "first"
        gate.set()
        with pytest.raises(StopAsyncIteration):
            await asyncio.wait_for(anext(events), timeout=1.0)
    finally:
        gate.set()
        await events.aclose()
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_async_inference_cancellation_closes_provider_stream() -> None:
    """Cancelling an in-flight graph call releases the upstream response."""

    gate = asyncio.Event()
    provider_stream = _DelayedSSEStream(gate)

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=provider_stream)

    events = _async_transport(handler).stream_json_events(
        {"model": "gpt-5.2", "stream": True}
    )
    await anext(events)
    pending = asyncio.create_task(anext(events))
    await asyncio.sleep(0)

    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert provider_stream.closed is True


@pytest.mark.asyncio
async def test_async_inference_rejects_oversized_body_before_network() -> None:
    """The async path preserves the guarded request-size boundary."""

    calls = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={})

    transport = _async_transport(
        handler,
        bounds=GuardedEgressBounds(max_request_bytes=16),
    )

    with pytest.raises(GuardedTransportError):
        await transport.request_json({"prompt": "x" * 64})
    assert calls == 0


def test_transport_enforces_fixed_target_redirect_proxy_tls_and_timeouts() -> None:
    """The request uses only guarded security settings and provider auth headers."""

    session = _Session()
    transport = _transport(session)

    response = transport.execute(
        "health",
        provider="openai",
        secret=ProviderSecret(provider="openai", value="sk-secret"),
    )

    assert response.status_code == 200
    assert response.body == b'{"data":[]}'
    assert len(response.audit_id) == 32
    assert session.trust_env is False
    assert session.closed is True
    assert session.response.closed is True
    assert session.calls == [
        {
            "method": "GET",
            "url": "https://api.openai.com/v1/models",
            "headers": {
                "accept": "application/json",
                "authorization": "Bearer sk-secret",
            },
            "json": None,
            "allow_redirects": False,
            "timeout": (1.0, 2.0),
            "stream": True,
            "verify": True,
        }
    ]


@pytest.mark.parametrize(
    "timeouts",
    [
        {"connect_seconds": 0},
        {"read_seconds": -1},
        {"total_seconds": float("nan")},
        {"total_seconds": float("inf")},
        {"connect_seconds": "1"},
        {"read_seconds": True},
        {"connect_seconds": 4, "total_seconds": 3},
        {"read_seconds": 4, "total_seconds": 3},
    ],
)
def test_timeout_configuration_rejects_invalid_values(
    timeouts: dict[str, Any],
) -> None:
    """Timeout controls accept only finite positive values within total duration."""

    with pytest.raises(ValueError):
        GuardedEgressTimeouts(**timeouts)  # type: ignore[arg-type]


def test_transport_enforces_total_duration(monkeypatch: pytest.MonkeyPatch) -> None:
    """Total wall-clock duration remains bounded in addition to socket timeouts."""

    ticks = iter((0.0, 4.0))
    monkeypatch.setattr(
        guarded_transport_module,
        "monotonic",
        lambda: next(ticks),
    )

    with pytest.raises(GuardedTransportError, match="timed out"):
        _transport(_Session()).execute(
            "health",
            provider="openai",
            secret=ProviderSecret(provider="openai", value="sk-secret"),
        )


def test_transport_rejects_oversized_request_before_send() -> None:
    """Bounded request bodies cannot reach the outbound session."""

    session = _Session()
    bounds = GuardedEgressBounds(max_request_bytes=16)

    with pytest.raises(GuardedTransportError):
        _transport(session, bounds=bounds).execute(
            "inference",
            provider="openai",
            secret=ProviderSecret(provider="openai", value="sk-secret"),
            json_body={"prompt": "x" * 64},
        )

    assert session.calls == []


def test_transport_builds_only_code_owned_anthropic_headers() -> None:
    """Typed Anthropic auth maps to the fixed protocol header set."""

    session = _Session()
    transport = _transport(session)

    transport.execute(
        "health",
        provider="anthropic",
        secret=ProviderSecret(provider="anthropic", value="sk-ant-secret"),
    )

    assert session.calls[0]["headers"] == {
        "accept": "application/json",
        "anthropic-version": "2023-06-01",
        "x-api-key": "sk-ant-secret",
    }


@pytest.mark.parametrize(
    ("preset_id", "base_url", "expected_url"),
    (
        (
            GPT_OSS_20B_PROVING_PRESET_ID,
            None,
            "https://gpt-oss.example.test/v1/models",
        ),
        (
            HUGGINGFACE_OPENAI_COMPATIBLE_PRESET_ID,
            None,
            "https://router.huggingface.co/v1/models",
        ),
        (
            NVIDIA_NIM_OPENAI_COMPATIBLE_PRESET_ID,
            None,
            "https://integrate.api.nvidia.com/v1/models",
        ),
        (
            OLLAMA_OPENAI_COMPATIBLE_PRESET_ID,
            "https://ollama.example.test/team",
            "https://ollama.example.test/team/v1/models",
        ),
        (
            VLLM_OPENAI_COMPATIBLE_PRESET_ID,
            "https://vllm.example.test/team",
            "https://vllm.example.test/team/v1/models",
        ),
        (
            CUSTOM_OPENAI_COMPATIBLE_PRESET_ID,
            "https://custom.example.test/team",
            "https://custom.example.test/team/v1/models",
        ),
    ),
)
def test_transport_builds_bearer_headers_for_reviewed_compatible_presets(
    preset_id: str,
    base_url: str | None,
    expected_url: str,
) -> None:
    """All reviewed OpenAI-compatible presets use the manifest-declared bearer auth."""

    session = _Session()
    registry = ConnectionOperationRegistry(
        env_getter=lambda name: (
            "https://gpt-oss.example.test"
            if name == GPT_OSS_20B_PROVING_BASE_URL_ENV
            else None
        )
    )
    operation_target = registry.resolve(
        LLMConnectionOperation.HEALTH,
        provider=preset_id,
        base_url=base_url,
    )

    _transport(session).execute(
        LLMConnectionOperation.HEALTH,
        provider=preset_id,
        secret=ProviderSecret(provider=preset_id, value="sk-preset"),
        operation_target=operation_target,
    )

    assert session.calls[0]["url"] == expected_url
    assert session.calls[0]["headers"] == {
        "accept": "application/json",
        "authorization": "Bearer sk-preset",
    }


@pytest.mark.parametrize("status_code", [301, 302, 307, 308])
def test_transport_rejects_redirect_responses(status_code: int) -> None:
    """Redirects are neither followed nor exposed as credential-forwarding targets."""

    session = _Session(
        _Response(
            status_code=status_code,
            headers={"location": "https://attacker.invalid/steal"},
        )
    )

    with pytest.raises(GuardedTransportError, match="audit_id") as exc_info:
        _transport(session).execute(
            "health",
            provider="openai",
            secret=ProviderSecret(provider="openai", value="sk-secret"),
        )

    assert "attacker.invalid" not in str(exc_info.value)
    assert "sk-secret" not in str(exc_info.value)


def test_transport_rejects_dns_rebinding_before_send() -> None:
    """A changed DNS answer prevents the request from reaching the session."""

    session = _Session()
    transport = _transport(
        session,
        dns_answers=[("93.184.216.34",), ("1.1.1.1",)],
    )

    with pytest.raises(GuardedTransportError):
        transport.execute(
            "health",
            provider="openai",
            secret=ProviderSecret(provider="openai", value="sk-secret"),
        )

    assert session.calls == []


@pytest.mark.parametrize(
    "response",
    [
        _Response(body=b"x" * 33, headers={"content-length": "33"}),
        _Response(body=b"x" * 33, headers={}),
        _Response(body=b"ok", headers={"x-large": "x" * 80}),
    ],
)
def test_transport_enforces_response_and_header_bounds(response: _Response) -> None:
    """Declared, streamed/decompressed, and header sizes remain bounded."""

    bounds = GuardedEgressBounds(
        max_response_bytes=32,
        max_header_bytes=64,
        read_chunk_bytes=8,
    )

    with pytest.raises(GuardedTransportError):
        _transport(_Session(response), bounds=bounds).execute(
            "health",
            provider="openai",
            secret=ProviderSecret(provider="openai", value="sk-secret"),
        )


def test_transport_sanitizes_upstream_exception_and_secret() -> None:
    """Transport exceptions expose only an opaque audit identifier."""

    session = _Session()
    session.error = requests.RequestException(
        "failed https://internal.invalid?token=secret with sk-secret"
    )

    with pytest.raises(GuardedTransportError) as exc_info:
        _transport(session).execute(
            "health",
            provider="openai",
            secret=ProviderSecret(provider="openai", value="sk-secret"),
        )

    message = str(exc_info.value)
    assert "audit_id=" in message
    assert "internal.invalid" not in message
    assert "secret" not in message
    assert exc_info.value.__cause__ is None


def test_transport_sanitizes_cleanup_failures() -> None:
    """Session cleanup cannot replace a guarded result with raw error details."""

    session = _Session()
    session.close_error = RuntimeError("close leaked https://internal.invalid?secret")

    with pytest.raises(GuardedTransportError) as exc_info:
        _transport(session).execute(
            "health",
            provider="openai",
            secret=ProviderSecret(provider="openai", value="sk-secret"),
        )

    message = str(exc_info.value)
    assert "audit_id=" in message
    assert "internal.invalid" not in message
    assert "secret" not in message


def test_transport_rejects_provider_mismatched_secret_before_request() -> None:
    """Credentials cannot be forwarded to a different provider origin."""

    session = _Session()
    with pytest.raises(GuardedTransportError):
        _transport(session).execute(
            "health",
            provider="openai",
            secret=ProviderSecret(provider="anthropic", value="sk-ant-secret"),
        )

    assert session.calls == []


def test_scaled_preset_inference_uses_authorized_target_and_rejects_private_dns() -> None:
    """Custom compatible inference keeps user endpoints behind guarded egress."""

    session = _Session()
    registry = ConnectionOperationRegistry()
    operation_target = registry.resolve(
        "inference",
        provider=CUSTOM_OPENAI_COMPATIBLE_PRESET_ID,
        base_url="https://llm.example.test/team",
    )

    with pytest.raises(GuardedTransportError):
        _transport(
            session,
            dns_answers=[("127.0.0.1",), ("127.0.0.1",)],
        ).execute(
            "inference",
            provider=CUSTOM_OPENAI_COMPATIBLE_PRESET_ID,
            secret=ProviderSecret(
                provider=CUSTOM_OPENAI_COMPATIBLE_PRESET_ID,
                value="sk-custom",
            ),
            json_body={
                "model": "team/model",
                "messages": [{"role": "user", "content": "ping"}],
            },
            operation_target=operation_target,
        )

    assert session.calls == []


def test_transport_allows_explicit_operator_loopback_override() -> None:
    """A trusted local override is used directly without ambient proxy inheritance."""

    session = _Session()
    registry = ConnectionOperationRegistry(
        env_getter={
            NVIDIA_NIM_BASE_URL_ENV: "http://127.0.0.1:4000"
        }.get
    )
    policy = FixedProviderEgressPolicy(
        dns_resolver=lambda _host, _port: ("127.0.0.1",)
    )
    transport = GuardedTransport(
        registry=registry,
        egress_policy=policy,
        session_factory=lambda: session,
    )

    transport.execute(
        LLMConnectionOperation.INFERENCE,
        provider=NVIDIA_NIM_OPENAI_COMPATIBLE_PRESET_ID,
        secret=ProviderSecret(
            provider=NVIDIA_NIM_OPENAI_COMPATIBLE_PRESET_ID,
            value="local-gateway-key",
        ),
        json_body={
            "model": "openai/gpt-oss-20b",
            "messages": [{"role": "user", "content": "ping"}],
        },
    )

    assert session.trust_env is False
    assert session.calls[0]["url"] == (
        "http://127.0.0.1:4000/v1/chat/completions"
    )
    assert session.calls[0]["headers"]["authorization"] == (
        "Bearer local-gateway-key"
    )
