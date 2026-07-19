"""Build shared OpenAI SDK client options for native and compatible adapters."""

from __future__ import annotations

from typing import Any

from ...core.exceptions import LLMConfigurationError


def openai_sdk_client_options(
    *,
    api_key: str | None,
    base_url: str | None,
    enforce_credentials: bool = True,
) -> dict[str, Any]:
    """Return explicit SDK construction options without relying on SDK env lookup."""

    options: dict[str, Any] = {"api_key": api_key}
    if base_url is not None:
        if (
            not isinstance(base_url, str)
            or not base_url
            or base_url != base_url.strip()
        ):
            raise LLMConfigurationError(
                "OpenAI client base URL must be a non-empty string",
                provider="OpenAI",
            )
        options["base_url"] = base_url
    if not enforce_credentials:
        options["_enforce_credentials"] = False
    return options


__all__ = ["openai_sdk_client_options"]
