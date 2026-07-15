"""Pure deterministic tool-output compression helpers for graph compaction.

This package owns side-effect-free adapters and helper functions that project
tool metadata and raw results into compact evidence. Modules here must not call
LLMs, inspect host files, or reach Docker, runner, backend, or runtime-provider
services.
"""
