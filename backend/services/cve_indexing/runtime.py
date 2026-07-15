"""Runtime singleton surface for CVE sync scheduler wiring.

Scope:
- Provides one process-wide scheduler instance reused by app lifecycle and API routes.

Boundary:
- Contains no scheduling logic; it only exposes shared runtime ownership.
"""

from backend.services.cve_indexing.scheduler import CveSyncScheduler

cve_sync_scheduler = CveSyncScheduler()

__all__ = ["cve_sync_scheduler"]
