"""Default runtime image names for the packaged Kali execution plane.

DrowAI publishes two architecture-specific runtime tags on Docker Hub:
``arm64-runtime`` for Apple Silicon / ARM Linux and ``amd64-runtime`` for
x86_64 Linux. Both images share the same Kali tool packages and execution-plane
runtime layers; only the CPU architecture differs.
"""

from __future__ import annotations

import platform

RUNTIME_IMAGE_REPOSITORY = "drowai/kali-pentesting"
ARM64_RUNTIME_TAG = "arm64-runtime"
AMD64_RUNTIME_TAG = "amd64-runtime"
ARM64_BASE_TAG = "arm64-base"
AMD64_BASE_TAG = "amd64-base"

ARM64_RUNTIME_IMAGE = f"{RUNTIME_IMAGE_REPOSITORY}:{ARM64_RUNTIME_TAG}"
AMD64_RUNTIME_IMAGE = f"{RUNTIME_IMAGE_REPOSITORY}:{AMD64_RUNTIME_TAG}"
ARM64_BASE_IMAGE = f"{RUNTIME_IMAGE_REPOSITORY}:{ARM64_BASE_TAG}"
AMD64_BASE_IMAGE = f"{RUNTIME_IMAGE_REPOSITORY}:{AMD64_BASE_TAG}"

# Backward-compatible defaults (ARM64 dev references).
DEFAULT_KALI_BASE_IMAGE = ARM64_BASE_IMAGE
DEFAULT_RUNTIME_IMAGE = ARM64_RUNTIME_IMAGE

_ARCH_ALIASES = {
    "arm64": "arm64",
    "aarch64": "arm64",
    "amd64": "amd64",
    "x86_64": "amd64",
}


def normalize_runtime_arch(raw: str | None) -> str:
    """Return ``arm64`` or ``amd64`` for supported architecture names."""
    key = (raw or "").strip().lower()
    normalized = _ARCH_ALIASES.get(key)
    if normalized is None:
        supported = ", ".join(sorted(_ARCH_ALIASES))
        raise ValueError(f"Unsupported runtime architecture `{raw}`. Expected one of: {supported}")
    return normalized


def runtime_base_image_for_arch(arch: str) -> str:
    """Docker reference for the vendor Kali base tag of an architecture."""
    normalized = normalize_runtime_arch(arch)
    if normalized == "arm64":
        return ARM64_BASE_IMAGE
    return AMD64_BASE_IMAGE


def runtime_image_for_arch(arch: str) -> str:
    """Docker reference for the packaged runtime image tag of an architecture."""
    normalized = normalize_runtime_arch(arch)
    if normalized == "arm64":
        return ARM64_RUNTIME_IMAGE
    return AMD64_RUNTIME_IMAGE


def runtime_platform_for_arch(arch: str) -> str:
    """Docker platform string for an architecture."""
    normalized = normalize_runtime_arch(arch)
    if normalized == "arm64":
        return "linux/arm64"
    return "linux/amd64"


def default_runtime_image_for_machine(uname_machine: str | None = None) -> str:
    """Pick the runtime image tag that matches the local CPU architecture."""
    raw_machine = uname_machine if uname_machine is not None else platform.machine()
    machine = raw_machine.strip().lower()
    if machine in {"aarch64", "arm64"}:
        return ARM64_RUNTIME_IMAGE
    return AMD64_RUNTIME_IMAGE


def is_digest_pinned_runtime_image(image_ref: str) -> bool:
    """Return whether an OCI image reference is pinned to an immutable digest."""
    return "@" in image_ref.strip()
