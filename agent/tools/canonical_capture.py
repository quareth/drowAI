"""Minimal shared contract for tool-owned canonical capture classification.

This module defines how each tool declares its internal capture strategy
(structured vs text, and which machine-readable format) without exposing
CLI-specific flags to backend or runtime layers.

Design rules:
- This contract is tool-owned and non-LLM-visible.
- Backend/runtime may consume only abstract properties (family, format),
  never concrete CLI flags like -oX, -oJ, or -jsonl.
- Text-native tools must not be forced into structured-native for uniformity.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class CaptureFamily(str, Enum):
    """Whether a tool natively produces structured or text output."""

    STRUCTURED_NATIVE = "structured_native"
    TEXT_NATIVE = "text_native"


class CanonicalCaptureFormat(str, Enum):
    """The canonical internal capture format a tool uses for parsing."""

    JSON = "json"
    JSONL = "jsonl"
    XML = "xml"
    TEXT = "text"


@dataclass(frozen=True)
class ToolCaptureContract:
    """Declares a tool's internal capture strategy.

    Attributes:
        family: Whether the tool is structured-native or text-native.
        canonical_format: The internal format the tool always uses for
            machine parsing and evidence retention.
    """

    family: CaptureFamily
    canonical_format: CanonicalCaptureFormat

    @property
    def is_structured(self) -> bool:
        return self.family is CaptureFamily.STRUCTURED_NATIVE

    @property
    def is_text(self) -> bool:
        return self.family is CaptureFamily.TEXT_NATIVE
