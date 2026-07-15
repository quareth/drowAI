"""HTTP retry/rate helper seam for web enumeration tools.

This module owns curl retry and transfer-rate flag assembly used by
HTTP request/download tools.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple


def build_retry_rate_curl_args(
    *,
    retries: Optional[int] = None,
    retry_delay: Optional[int] = None,
    retry_max_time: Optional[int] = None,
    retry_connrefused: bool = False,
    limit_rate: Optional[str] = None,
) -> Tuple[List[str], Dict[str, Any]]:
    """Build curl retry/rate argv and a compact applied-config metadata object."""
    cmd: List[str] = []
    applied: Dict[str, Any] = {}

    if retries is not None:
        cmd.extend(["--retry", str(retries)])
        applied["retries"] = retries
    if retry_delay is not None:
        cmd.extend(["--retry-delay", str(retry_delay)])
        applied["retry_delay"] = retry_delay
    if retry_max_time is not None:
        cmd.extend(["--retry-max-time", str(retry_max_time)])
        applied["retry_max_time"] = retry_max_time
    if retry_connrefused:
        cmd.append("--retry-connrefused")
        applied["retry_connrefused"] = True
    if limit_rate:
        cmd.extend(["--limit-rate", limit_rate])
        applied["limit_rate"] = limit_rate

    return cmd, applied
