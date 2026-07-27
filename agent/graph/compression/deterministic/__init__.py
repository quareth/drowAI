"""Pure deterministic helper package for graph compact-output assembly.

This package owns side-effect-free generic metadata, artifact, evidence, and
error helpers used by the compressor. Modules here must not register adapters,
call LLMs, inspect host files, or reach Docker, runner, backend, or
runtime-provider services.
"""
