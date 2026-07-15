"""Baseline characterization tests for agent tool schema contracts.

These tests lock current JSON schema output and high-risk validation behavior
before the tool schema contracts are mechanically moved into domain modules.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Type

import pytest
from pydantic import BaseModel, ValidationError

from agent.tools.artifact.contracts import ArtifactReadArgs, ArtifactSearchArgs
from agent.tools.information_gathering.web_enumeration.contracts import (
    HttpDownloadArgs,
    HttpRequestArgs,
)
from agent.tools.shell.contracts import (
    ShellCommandResult,
    ShellExecArgs,
    ShellScriptArgs,
)
from agent.tools.filesystem.contracts import (
    FilesystemEntry,
    FsAppendArgs,
    FsCopyArgs,
    FsDeleteArgs,
    FsEditLinesArgs,
    FsEditResult,
    FsFindArgs,
    FsFindResult,
    FsListArgs,
    FsListResult,
    FsMakeDirArgs,
    FsMoveArgs,
    FsMutationResult,
    FsReadArgs,
    FsReadResult,
    FsSearchTextArgs,
    FsSearchTextResult,
    FsStatArgs,
    FsWriteArgs,
    TextMatch,
    WorkspacePathArgs,
)


SCHEMA_DIGESTS: dict[str, tuple[Type[BaseModel], str]] = {
    "HttpRequestArgs": (
        HttpRequestArgs,
        "9d522e1487dade44d59db2cf374309d50958d0e17939cfeece61371902017663",
    ),
    "HttpDownloadArgs": (
        HttpDownloadArgs,
        "5cc928cdae3d7dfef51e47872563506b80ba1d216665ea74d2d1e0fdfa30c8a6",
    ),
    "ArtifactSearchArgs": (
        ArtifactSearchArgs,
        "a1b2ce7062e851a74b7e93af81188b892660a9894b5026743f362d09bb3def0c",
    ),
    "ArtifactReadArgs": (
        ArtifactReadArgs,
        "34308204cabd46bf6e9343b26fa6cd939ca6462b5ca197cac54b02ad17759e75",
    ),
    "WorkspacePathArgs": (
        WorkspacePathArgs,
        "2fc2f88a193e1a2b2c3f8ade00d6f6023c6d05e61600a377c18fa1a80976a238",
    ),
    "FsReadArgs": (
        FsReadArgs,
        "c1ff718f69ac5ada7c55f431ba0ad4d07068a9d88a20d4a859914a8b70914192",
    ),
    "FsWriteArgs": (
        FsWriteArgs,
        "957a7a74b6ea9891d5c95b1ce3008783c7691e704b72ad36bddbf2bbcadee3ed",
    ),
    "FsAppendArgs": (
        FsAppendArgs,
        "073606730da833bddd7fe409de30b85cf7caa567adec7b5bd906a7b7c388ebbc",
    ),
    "FsDeleteArgs": (
        FsDeleteArgs,
        "99afa83f91946b206bdd8d6b173a4b383166a2b957e5b0268533124375e478a2",
    ),
    "FsMakeDirArgs": (
        FsMakeDirArgs,
        "6ad37be0518ccb0f13538ea66546cb0417a60e3f1dd140c799adb10d1b7a9dcf",
    ),
    "FsListArgs": (
        FsListArgs,
        "d4f26bd62128a326aa0b779a9469c8d4b4e253c59fd182f6abc92ea54b1c970b",
    ),
    "FsMoveArgs": (
        FsMoveArgs,
        "2b6da1f3b395467cb3a655f533b07812e1fef7a23f1751b1bfd1f5e3582e75f8",
    ),
    "FsCopyArgs": (
        FsCopyArgs,
        "c5bc3ff1a547e1b82750ab5521b6523b5e13f6f5a0a34282211e236d29c11f51",
    ),
    "FsStatArgs": (
        FsStatArgs,
        "b2191d99caccfaa9c3d8eb2638115d33112d4e137ba93d5de87f9db2af53e55b",
    ),
    "FsFindArgs": (
        FsFindArgs,
        "0735deee899c940329a91472a43cd6ca6781a0685d2ca6ef09642a7b0ea8256a",
    ),
    "FsEditLinesArgs": (
        FsEditLinesArgs,
        "e2233488b343a1886e8c0240ce06f70d445305e5b6ba69302a61e85c5e7e52df",
    ),
    "FsSearchTextArgs": (
        FsSearchTextArgs,
        "8572f4e51db73195064c86e1c970269c5ee5d8cb1f66b06202a6589b14d33583",
    ),
    "FilesystemEntry": (
        FilesystemEntry,
        "4720fb1c280ae6cee8891fd9297eed9cf4c367c80f5266d0603760246839f00f",
    ),
    "FsReadResult": (
        FsReadResult,
        "80af2371416deb3e059236a925a3df8746abc8f324523df8a805b42c7a9529a0",
    ),
    "FsListResult": (
        FsListResult,
        "21e472eda36b885ec9230fd511125887cac175628be94e3b5e729d4674bedeb6",
    ),
    "FsFindResult": (
        FsFindResult,
        "752f1ed3fffb61bc39eb1dd5a3101b874e7d61dbec67b4c3efda49b0844ba7fe",
    ),
    "TextMatch": (
        TextMatch,
        "c0f90b6ef24332ebf4862a39ff2028b4408b34c8dc721d1b331df1aae4d7d175",
    ),
    "FsSearchTextResult": (
        FsSearchTextResult,
        "ca140b9323c491f14eb13b65f8ffa4fef07cb9123c4d203100f2739c9842f411",
    ),
    "FsEditResult": (
        FsEditResult,
        "4f1b3955c706eac4c3888b88f964369e3d686296d01c41b5be7e25bd248cf38f",
    ),
    "FsMutationResult": (
        FsMutationResult,
        "c514126f279b9bc1b3ae2ae0dcec589e888ca52910fc0812653414c21209ab8f",
    ),
    "ShellExecArgs": (
        ShellExecArgs,
        "0a7e1708a809532324a6f8e3025777109f086ad0ce584ea061d98fc6e29ccd96",
    ),
    "ShellScriptArgs": (
        ShellScriptArgs,
        "83f2cc80fec45715c9572aae3cbb40b12435865b4233f466ed05c73191f96491",
    ),
    "ShellCommandResult": (
        ShellCommandResult,
        "5c12d55742f8392433aeb21d85858f1a6c8a270a91ad6349d30f94286d09e4be",
    ),
}


def _schema_digest(model: Type[BaseModel]) -> str:
    schema_json = json.dumps(
        model.model_json_schema(),
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(schema_json.encode("utf-8")).hexdigest()


def _assert_validation_error(
    model: Type[BaseModel],
    values: dict[str, Any],
    message: str,
) -> None:
    with pytest.raises(ValidationError) as exc_info:
        model(**values)

    assert message in str(exc_info.value)


@pytest.mark.parametrize(
    ("schema_name", "model", "expected_digest"),
    [(name, model, digest) for name, (model, digest) in SCHEMA_DIGESTS.items()],
)
def test_planned_move_schema_json_snapshots(
    schema_name: str,
    model: Type[BaseModel],
    expected_digest: str,
) -> None:
    assert _schema_digest(model) == expected_digest, schema_name


@pytest.mark.parametrize("model", [HttpRequestArgs, HttpDownloadArgs])
def test_http_target_validation_rejects_invalid_urls(model: Type[BaseModel]) -> None:
    required = {"output_path": "downloads/out.bin"} if model is HttpDownloadArgs else {}

    _assert_validation_error(
        model,
        {"target": "ftp://example.test", **required},
        "target scheme must be http or https",
    )
    _assert_validation_error(
        model,
        {"target": "/relative/path", **required},
        "target scheme must be http or https",
    )


def test_http_request_body_sources_are_mutually_exclusive() -> None:
    _assert_validation_error(
        HttpRequestArgs,
        {
            "target": "https://example.test",
            "body": "alpha",
            "body_file_path": "payload.bin",
        },
        "body, body_file_path, and body_base64 are mutually exclusive",
    )


@pytest.mark.parametrize("model", [HttpRequestArgs, HttpDownloadArgs])
def test_http_connection_control_validation(model: Type[BaseModel]) -> None:
    required = {"output_path": "downloads/out.bin"} if model is HttpDownloadArgs else {}

    _assert_validation_error(
        model,
        {"target": "https://example.test", "resolve": ["example.test:443"], **required},
        "resolve entries must be host:port:address",
    )
    _assert_validation_error(
        model,
        {
            "target": "https://example.test",
            "connect_to": ["example.test:443:127.0.0.1"],
            **required,
        },
        "connect_to entries must be host1:port1:host2:port2",
    )


@pytest.mark.parametrize("model", [HttpRequestArgs, HttpDownloadArgs])
def test_http_auth_mtls_and_retry_validation(model: Type[BaseModel]) -> None:
    required = {"output_path": "downloads/out.bin"} if model is HttpDownloadArgs else {}

    _assert_validation_error(
        model,
        {"target": "https://example.test", "username": "alice", **required},
        "auth_mode must be set when username/password/bearer_token is provided",
    )
    _assert_validation_error(
        model,
        {"target": "https://example.test", "auth_mode": "basic", "username": "alice", **required},
        "auth_mode=basic requires both username and password",
    )
    _assert_validation_error(
        model,
        {
            "target": "https://example.test",
            "auth_mode": "bearer",
            "bearer_token": "token",
            "username": "alice",
            **required,
        },
        "auth_mode=bearer cannot be combined with username/password",
    )
    _assert_validation_error(
        model,
        {"target": "https://example.test", "client_key_path": "client.key", **required},
        "client_key_path requires client_cert_path",
    )
    _assert_validation_error(
        model,
        {"target": "https://example.test", "retry_delay": 1, **required},
        "retry_delay/retry_max_time/retry_connrefused require retries",
    )


def test_http_download_integrity_and_speed_validation() -> None:
    _assert_validation_error(
        HttpDownloadArgs,
        {
            "target": "https://example.test/file.bin",
            "output_path": "downloads/file.bin",
            "expected_sha256": "not-a-digest",
        },
        "expected_sha256 must be a 64-character hex digest",
    )
    _assert_validation_error(
        HttpDownloadArgs,
        {
            "target": "https://example.test/file.bin",
            "output_path": "downloads/file.bin",
            "speed_limit": 1024,
        },
        "speed_limit and speed_time must be provided together",
    )

    digest = "A" * 64
    args = HttpDownloadArgs(
        target="https://example.test/file.bin",
        output_path="downloads/file.bin",
        expected_sha256=digest,
    )
    assert args.expected_sha256 == digest.lower()


def test_filesystem_line_and_bound_validation() -> None:
    _assert_validation_error(
        FsReadArgs,
        {"path": "notes.txt", "offset": 0},
        "Input should be greater than or equal to 1",
    )
    _assert_validation_error(
        FsEditLinesArgs,
        {"path": "notes.txt", "start_line": 0},
        "Input should be greater than or equal to 1",
    )
    _assert_validation_error(
        FsListArgs,
        {"path": ".", "max_results": 20_001},
        "Input should be less than or equal to 20000",
    )
    _assert_validation_error(
        FsFindArgs,
        {"max_depth": 26},
        "Input should be less than or equal to 25",
    )
    _assert_validation_error(
        FsSearchTextArgs,
        {"query": "token", "max_file_bytes": 5_000_001},
        "Input should be less than or equal to 5000000",
    )


def test_filesystem_read_alias_post_init_behavior() -> None:
    args = FsReadArgs(path="logs/app.log", search="ERROR", offset=15)

    assert args.grep_pattern == "ERROR"
    assert args.start_line == 15


def test_shell_transport_and_interpreter_validation() -> None:
    _assert_validation_error(
        ShellExecArgs,
        {"command": "id", "transport": "direct"},
        "Input should be 'file-comm' or 'pty'",
    )
    _assert_validation_error(
        ShellScriptArgs,
        {"script": "echo test", "transport": "direct"},
        "Input should be 'file-comm' or 'pty'",
    )
    _assert_validation_error(
        ShellScriptArgs,
        {"script": "echo test", "interpreter": "ruby"},
        "Input should be 'bash', 'sh', 'python3' or 'powershell'",
    )
