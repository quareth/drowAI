"""HTTP session/cookie helper seam for web enumeration tools.

This module owns cookie/session curl flag assembly shared by HTTP request and
download tools.
"""

from __future__ import annotations

from typing import List, Optional, Tuple


def build_session_curl_args(
    *,
    cookie: Optional[str] = None,
    cookie_file: Optional[str] = None,
    cookie_jar: Optional[str] = None,
    persist_cookies: bool = False,
    default_cookie_jar: Optional[str] = None,
) -> Tuple[List[str], bool]:
    """Build curl argv flags for cookie input and persistence.

    Returns:
        - argv additions for curl session flags
        - whether cookie persistence is active
    """
    cmd: List[str] = []
    if cookie:
        cmd.extend(["--cookie", cookie])
    elif cookie_file:
        cmd.extend(["--cookie", cookie_file])

    jar_target = cookie_jar
    if persist_cookies and not jar_target:
        jar_target = cookie_file or default_cookie_jar
    if jar_target:
        cmd.extend(["--cookie-jar", jar_target])

    return cmd, bool(jar_target)
