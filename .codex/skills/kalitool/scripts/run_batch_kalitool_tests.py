"""Run kalitool schema tests for every unchecked tool in artifacts/kalitool-tool-state.md.

For each tool: runs --params minimal then --params full; on both success, marks
the tool completed in the state file. Auth: --jwt-token or --username + --password.
Does not log or echo passwords.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path


def _repo_root() -> Path:
    here = Path(__file__).resolve()
    return here.parents[4]


ROOT = _repo_root()
STATE_FILE = ROOT / "artifacts" / "kalitool-tool-state.md"
SCRIPT = ROOT / ".codex" / "skills" / "kalitool" / "scripts" / "run_real_kali_tool_schema_test.py"


def unchecked_tool_ids(content: str) -> list[str]:
    """Extract tool IDs from lines like '- [ ] `category.sub.tool_name`' (no trailing note)."""
    ids_list: list[str] = []
    for m in re.finditer(r"^\s*-\s+\[\s\]\s+`([a-zA-Z0-9_.]+)`\s*$", content, re.MULTILINE):
        ids_list.append(m.group(1))
    return ids_list


def mark_completed(content: str, tool_id: str) -> str:
    """Replace the unchecked line for tool_id with checked (minimal+full)."""
    old_line = f"- [ ] `{tool_id}`"
    new_line = f"- [x] `{tool_id}` (minimal+full)"
    if old_line in content:
        content = content.replace(old_line + "\n", new_line + "\n", 1)
    return content


def mark_failed(content: str, tool_id: str, reason: str) -> str:
    """Replace the unchecked line for tool_id with a failure note (keep unchecked)."""
    old_line = f"- [ ] `{tool_id}`"
    new_line = f"- [ ] `{tool_id}` (fail: {reason})"
    if old_line in content:
        content = content.replace(old_line + "\n", new_line + "\n", 1)
    return content


def run_one(tool_id: str, params: str, auth_args: list[str], api_base_url: str, timeout: int) -> tuple[bool, str]:
    """Run run_real_kali_tool_schema_test.py once. Return (success, message)."""
    cmd = [
        sys.executable,
        str(SCRIPT),
        "--tool-id", tool_id,
        "--params", params,
        "--api-base-url", api_base_url,
    ] + auth_args
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=str(ROOT))
        out = (r.stdout or "").strip()
        err = (r.stderr or "").strip()
        if r.returncode != 0:
            return False, err or out or f"exit {r.returncode}"
        return True, out
    except subprocess.TimeoutExpired:
        return False, "timeout"
    except Exception as e:
        return False, str(e)


def main() -> int:
    ap = argparse.ArgumentParser(description="Batch run kalitool tests for all unchecked tools.")
    ap.add_argument("--username", default=None, help="Username for login")
    ap.add_argument("--password", default=None, help="Password (not echoed)")
    ap.add_argument("--jwt-token", default=None, help="Bearer token (alternative to username/password)")
    ap.add_argument("--api-base-url", default="http://localhost:8000", help="Backend URL")
    ap.add_argument("--timeout", type=int, default=300, help="Per-run timeout in seconds")
    ap.add_argument("--dry-run", action="store_true", help="Only list unchecked tools")
    args = ap.parse_args()

    auth_args: list[str] = []
    if args.jwt_token:
        auth_args = ["--jwt-token", args.jwt_token]
    elif args.username and args.password:
        auth_args = ["--username", args.username, "--password", args.password]
    else:
        print("Provide --jwt-token or both --username and --password.", file=sys.stderr)
        return 1

    if not STATE_FILE.exists():
        print(f"State file not found: {STATE_FILE}", file=sys.stderr)
        return 1

    content = STATE_FILE.read_text(encoding="utf-8")
    todo = unchecked_tool_ids(content)
    if not todo:
        print("No unchecked tools.")
        return 0

    if args.dry_run:
        for t in todo:
            print(t)
        return 0

    completed = 0
    failed: list[tuple[str, str]] = []

    for i, tool_id in enumerate(todo, 1):
        print(f"[{i}/{len(todo)}] {tool_id} ...", flush=True)
        ok_min, _ = run_one(tool_id, "minimal", auth_args, args.api_base_url, args.timeout)
        if not ok_min:
            failed.append((tool_id, "minimal run failed"))
            content = mark_failed(content, tool_id, "minimal run failed")
            STATE_FILE.write_text(content, encoding="utf-8")
            continue
        ok_full, msg = run_one(tool_id, "full", auth_args, args.api_base_url, args.timeout)
        if not ok_full:
            failed.append((tool_id, msg or "full run failed"))
            content = mark_failed(content, tool_id, msg or "full run failed")
            STATE_FILE.write_text(content, encoding="utf-8")
            continue
        content = mark_completed(content, tool_id)
        STATE_FILE.write_text(content, encoding="utf-8")
        completed += 1

    print()
    print(f"Completed: {completed}")
    print(f"Failed: {len(failed)}")
    print(f"State file: {STATE_FILE}")
    for tid, reason in failed:
        print(f"  - {tid}: {reason}")

    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
