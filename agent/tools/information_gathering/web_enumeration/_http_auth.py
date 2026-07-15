"""HTTP authentication helper seam for web enumeration tools.

This module owns curl auth flag assembly for request/download tools.
"""

from __future__ import annotations

from typing import List, Literal, Optional, Tuple


AuthMode = Literal["none", "basic", "bearer"]


def build_auth_curl_args(
    *,
    auth_mode: AuthMode = "none",
    username: Optional[str] = None,
    password: Optional[str] = None,
    bearer_token: Optional[str] = None,
) -> Tuple[List[str], str]:
    """Build curl argv flags for explicit auth modes."""
    if auth_mode == "basic":
        return ["--user", f"{username}:{password}"], "basic"
    if auth_mode == "bearer":
        return ["--oauth2-bearer", bearer_token or ""], "bearer"
    return [], "none"
