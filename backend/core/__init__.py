"""Backend core utilities shared across all backend services.

Scope:
- Cross-cutting infrastructure: caching, rate limiting, time normalization.

Boundary:
- No business logic, no ORM models, no router behavior.
"""
