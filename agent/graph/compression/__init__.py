"""Compression package exports for compact tool-output handling.

Keep this package import cycle-safe: schema types are imported eagerly, while
compressor functions are forwarded lazily so schema-only consumers do not
trigger tool-processor imports during module initialization.
"""

from __future__ import annotations

from .schema import (
    TOOL_OUTPUT_COMPRESSOR_USAGE_SOURCE,
    ArtifactReference,
    CompactToolOutput,
    CompressionMetadata,
    ToolOutputCompressionResult,
)


async def compress_tool_output(*args, **kwargs):
    """Lazy forwarder for compact tool-output compression."""
    from .compressor import compress_tool_output as _compress_tool_output

    return await _compress_tool_output(*args, **kwargs)


def compact_output_size_bytes(*args, **kwargs):
    """Lazy forwarder for compact envelope size calculation."""
    from .compressor import compact_output_size_bytes as _compact_output_size_bytes

    return _compact_output_size_bytes(*args, **kwargs)

__all__ = [
    "ArtifactReference",
    "TOOL_OUTPUT_COMPRESSOR_USAGE_SOURCE",
    "CompressionMetadata",
    "CompactToolOutput",
    "ToolOutputCompressionResult",
    "compress_tool_output",
    "compact_output_size_bytes",
]
