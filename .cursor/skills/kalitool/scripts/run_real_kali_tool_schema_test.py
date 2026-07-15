"""Execute a real-Kali tool schema test and emit a markdown report.

This script performs an end-to-end validation for a single tool:
1) Authenticate (JWT or login)
2) Create a real task/container
3) Build safe parameters from real tool schema
4) Execute via FileComm in the task workspace
5) Write markdown report
6) Cleanup task by default
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


def _repo_root() -> Path:
    here = Path(__file__).resolve()
    # .../.cursor/skills/<skill>/scripts/<file>.py -> repo root is parents[4]
    return here.parents[4]


ROOT = _repo_root()
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.config.workspace_config import WorkspaceConfig  # noqa: E402
from agent.communication.file_comm import FileCommAgent  # noqa: E402
from agent.tools.tool_registry import get_tool_metadata  # noqa: E402


# Safe optional targets by type (exact param-name match only).
SAFE_IP = "127.0.0.1"
SAFE_HOSTNAME = "localhost"
SAFE_DOMAIN = "example.com"
SAFE_URL = "http://localhost"
SAFE_RANGE = "127.0.0.1-127.255.255.255"
SAFE_SUBNET = "127.0.0.0/24"

# Param names treated as target-like (exact match only to avoid e.g. "script" -> ip).
TARGET_LIKE_KEYS = frozenset({
    "target", "host", "hosts", "hostname", "domain", "url",
    "ip", "address", "src_ip", "dst_ip", "adapter_ip",
    "range", "ip_range", "network_range", "cidr", "subnet", "exclude",
})


def _http_json(
    method: str,
    url: str,
    token: str | None = None,
    payload: dict[str, Any] | None = None,
    timeout: int = 20,
) -> dict[str, Any]:
    data = None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url=url, data=data, method=method.upper(), headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8") if exc.fp else ""
        msg = body or str(exc)
        raise RuntimeError(f"HTTP {exc.code} {url}: {msg}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Request failed for {url}: {exc}") from exc


def _resolve_token(args: argparse.Namespace) -> tuple[str, str]:
    if args.jwt_token:
        return args.jwt_token, "jwt"
    if not args.username or not args.password:
        raise RuntimeError("Provide --jwt-token or both --username and --password.")
    login_url = f"{args.api_base_url.rstrip('/')}/api/auth/login"
    login_res = _http_json(
        "POST",
        login_url,
        payload={"username": args.username, "password": args.password},
    )
    token = login_res.get("access_token")
    if not token:
        raise RuntimeError("Login succeeded without access_token.")
    return token, "login"


def _create_task(api_base_url: str, token: str, tool_id: str) -> int:
    task_name = f"skill-tooltest-{tool_id.replace('.', '-')}-{int(time.time())}"
    payload = {
        "name": task_name,
        "description": f"Real Kali schema test for {tool_id}",
        "scope": "localhost-only-safe-target-testing",
    }
    url = f"{api_base_url.rstrip('/')}/api/tasks/"
    created = _http_json("POST", url, token=token, payload=payload)
    task_id = created.get("id")
    if not task_id:
        raise RuntimeError(f"Task creation response missing id: {created}")
    return int(task_id)


def _get_task(api_base_url: str, token: str, task_id: int) -> dict[str, Any]:
    url = f"{api_base_url.rstrip('/')}/api/tasks/{task_id}"
    return _http_json("GET", url, token=token)


def _get_container_status(api_base_url: str, token: str, task_id: int) -> dict[str, Any]:
    url = f"{api_base_url.rstrip('/')}/api/tasks/{task_id}/container/status"
    return _http_json("GET", url, token=token)


def _wait_for_runtime(api_base_url: str, token: str, task_id: int, timeout_s: int) -> tuple[dict[str, Any], dict[str, Any]]:
    deadline = time.time() + timeout_s
    last_task: dict[str, Any] = {}
    last_container: dict[str, Any] = {}
    while time.time() < deadline:
        last_task = _get_task(api_base_url, token, task_id)
        last_container = _get_container_status(api_base_url, token, task_id)
        status = str(last_task.get("status", "")).lower()
        c_exists = bool(last_container.get("container_exists"))
        c_status = str(last_container.get("status", "")).lower()
        if status == "running" and c_exists and c_status in {"running", "up", "simulated"}:
            return last_task, last_container
        if status in {"failed", "stopped", "timeout"}:
            raise RuntimeError(f"Task entered terminal non-running status: {status}")
        time.sleep(2.0)
    raise TimeoutError(f"Timed out waiting for task/container readiness for task {task_id}. Last status={last_task.get('status')}, container={last_container}")


def _pick_default_for_property(key: str, prop: dict[str, Any]) -> Any:
    lk = key.lower()
    if "enum" in prop and isinstance(prop["enum"], list) and prop["enum"]:
        return prop["enum"][0]
    if "default" in prop:
        return prop["default"]
    if lk in TARGET_LIKE_KEYS:
        if lk in ("ip", "address", "src_ip", "dst_ip", "adapter_ip"):
            return SAFE_IP
        if lk == "url":
            return SAFE_URL
        if lk == "hostname":
            return SAFE_HOSTNAME
        if lk in ("target", "host", "hosts", "domain"):
            return SAFE_DOMAIN
        if lk in ("range", "ip_range", "network_range", "exclude"):
            return SAFE_RANGE
        if lk in ("cidr", "subnet"):
            return SAFE_SUBNET

    ptype = prop.get("type")
    if ptype is None and "anyOf" in prop:
        for node in prop.get("anyOf", []):
            if isinstance(node, dict) and node.get("type") and node.get("type") != "null":
                ptype = node.get("type")
                break

    if ptype == "string":
        return "test"
    if ptype in {"integer", "number"}:
        if isinstance(prop.get("minimum"), (int, float)):
            return int(prop["minimum"]) if ptype == "integer" else float(prop["minimum"])
        return 1 if ptype == "integer" else 1.0
    if ptype == "boolean":
        return False
    if ptype == "array":
        items = prop.get("items") if isinstance(prop.get("items"), dict) else {}
        if "enum" in items and items["enum"]:
            return [items["enum"][0]]
        return []
    if ptype == "object":
        return {}
    return "test"


def _enforce_safe_targets(params: dict[str, Any]) -> dict[str, Any]:
    out = dict(params)
    for key, value in list(out.items()):
        lk = key.lower()
        if lk not in TARGET_LIKE_KEYS:
            continue
        if not isinstance(value, str):
            continue
        if lk in ("ip", "address", "src_ip", "dst_ip", "adapter_ip"):
            out[key] = SAFE_IP
        elif lk == "url":
            out[key] = SAFE_URL
        elif lk == "hostname":
            out[key] = SAFE_HOSTNAME
        elif lk in ("target", "host", "hosts", "domain"):
            if value.strip().startswith(("http://", "https://")):
                out[key] = SAFE_URL
            else:
                out[key] = SAFE_DOMAIN
        elif lk in ("range", "ip_range", "network_range", "exclude"):
            out[key] = SAFE_RANGE
        elif lk in ("cidr", "subnet"):
            out[key] = SAFE_SUBNET
    return out


def _pick_full_for_property(key: str, prop: dict[str, Any]) -> Any:
    """Pick a value for optional/full params from JSON schema property."""
    lk = key.lower()
    if "enum" in prop and isinstance(prop["enum"], list) and len(prop["enum"]) > 1:
        return prop["enum"][1]
    if "enum" in prop and isinstance(prop["enum"], list) and prop["enum"]:
        return prop["enum"][0]
    if "default" in prop:
        return prop["default"]
    if lk in TARGET_LIKE_KEYS:
        if lk in ("ip", "address", "src_ip", "dst_ip", "adapter_ip"):
            return SAFE_IP
        if lk == "url":
            return SAFE_URL
        if lk == "hostname":
            return SAFE_HOSTNAME
        if lk in ("target", "host", "hosts", "domain"):
            return SAFE_DOMAIN
        if lk in ("range", "ip_range", "network_range", "exclude"):
            return SAFE_RANGE
        if lk in ("cidr", "subnet"):
            return SAFE_SUBNET

    ptype = prop.get("type")
    if ptype is None and "anyOf" in prop:
        for node in prop.get("anyOf", []):
            if isinstance(node, dict) and node.get("type") and node.get("type") != "null":
                ptype = node.get("type")
                break

    if ptype == "string":
        if key == "ports":
            return "80,443"
        return "test"
    if ptype in {"integer", "number"}:
        gt = prop.get("gt")
        ge = prop.get("minimum")
        if ge is not None:
            return int(ge) if ptype == "integer" else float(ge)
        if gt is not None:
            return (int(gt) + 1) if ptype == "integer" else (float(gt) + 1.0)
        return 100 if ptype == "integer" else 100.0
    if ptype == "boolean":
        return True
    if ptype == "array":
        items = prop.get("items")
        if isinstance(items, dict) and items.get("enum"):
            return [items["enum"][0]] if items["enum"] else []
        return []
    if ptype == "object":
        return {}
    return "test"


def _build_safe_params(tool_id: str, full: bool = False) -> tuple[dict[str, Any], dict[str, Any]]:
    meta = get_tool_metadata(tool_id)
    schema = meta.get("args_schema") or {}
    properties = schema.get("properties") or {}
    required = schema.get("required") or []

    params: dict[str, Any] = {}
    for field in required:
        prop = properties.get(field, {})
        params[field] = _pick_default_for_property(field, prop if isinstance(prop, dict) else {})

    if full:
        for field_name, prop in properties.items():
            if field_name in params:
                continue
            if not isinstance(prop, dict):
                continue
            if prop.get("deprecated") is True:
                continue
            params[field_name] = _pick_full_for_property(field_name, prop)

    if "target" in properties and "target" not in params:
        params["target"] = SAFE_DOMAIN

    params = _enforce_safe_targets(params)
    return params, schema


async def _execute_via_filecomm(task_id: int, tool_id: str, params: dict[str, Any], timeout_s: int) -> dict[str, Any]:
    workspace = WorkspaceConfig.get_task_workspace_path(task_id)
    comm = FileCommAgent(str(workspace))
    cmd_id = await comm.send_command({"tool": tool_id, "args": params, "timeout": timeout_s})
    result = await comm.wait_for_result(cmd_id, timeout=timeout_s)
    return result


def _excerpt(text: str, max_chars: int = 3000) -> str:
    if not text:
        return ""
    text = text.strip()
    return text if len(text) <= max_chars else f"{text[:max_chars]}\n... (truncated)"


def _write_report(
    report_path: Path,
    tool_id: str,
    auth_method: str,
    auth_status: str,
    task_id: int | None,
    container_status: str,
    schema: dict[str, Any],
    params: dict[str, Any],
    result: dict[str, Any] | None,
    verdict: str,
    reason: str,
) -> None:
    required_count = len(schema.get("required") or [])
    optional_count = max(0, len((schema.get("properties") or {}).keys()) - required_count)
    success = result.get("success") if result else False
    exit_code = result.get("exit_code") if result else "n/a"
    stdout = _excerpt((result or {}).get("stdout", ""))
    stderr = _excerpt((result or {}).get("stderr", ""))

    content = f"""# Tool Schema Runtime Test Report

## Tool
- tool_id: `{tool_id}`
- mode: `strict-real-kali`

## Authentication
- method: `{auth_method}`
- status: `{auth_status}`

## Runtime
- task_id: `{task_id if task_id is not None else "n/a"}`
- container_status: `{container_status}`

## Schema Summary
- required_fields: `{required_count}`
- optional_fields: `{optional_count}`

## Parameters Used
```json
{json.dumps(params, indent=2)}
```

## Execution Result
- success: `{success}`
- exit_code: `{exit_code}`

### stdout (excerpt)
```text
{stdout}
```

### stderr (excerpt)
```text
{stderr}
```

## Verdict
- status: `{verdict}`
- reason: `{reason}`
"""
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(content, encoding="utf-8")


def _cleanup_task(api_base_url: str, token: str, task_id: int) -> None:
    url = f"{api_base_url.rstrip('/')}/api/tasks/{task_id}"
    _http_json("DELETE", url, token=token)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run real-Kali tool schema test.")
    parser.add_argument("--tool-id", required=True, help="Tool registry id")
    parser.add_argument("--api-base-url", default="http://localhost:8000", help="Backend base URL")
    parser.add_argument("--jwt-token", default=None, help="Bearer token (optional if username/password used)")
    parser.add_argument("--username", default=None, help="Username for /api/auth/login")
    parser.add_argument("--password", default=None, help="Password for /api/auth/login")
    parser.add_argument("--report-path", default=None, help="Markdown report path")
    parser.add_argument("--startup-timeout", type=int, default=180, help="Task/container startup timeout (seconds)")
    parser.add_argument("--exec-timeout", type=int, default=90, help="Tool execution timeout (seconds)")
    parser.add_argument("--keep-on-failure", action="store_true", help="Do not delete temp task when failing")
    parser.add_argument("--params", choices=("minimal", "full"), default="minimal", help="Use minimal (required only) or full (all applicable) parameters")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    suffix = "-full" if getattr(args, "params", "minimal") == "full" else ""
    report_path = Path(args.report_path) if args.report_path else ROOT / "artifacts" / f"tool-schema-test-{args.tool_id.replace('.', '_')}{suffix}.md"

    auth_method = "unknown"
    auth_status = "failed"
    task_id: int | None = None
    container_status = "unknown"
    schema: dict[str, Any] = {}
    params: dict[str, Any] = {}
    result: dict[str, Any] | None = None
    verdict = "FAIL"
    reason = "Unhandled failure"
    token = ""

    try:
        token, auth_method = _resolve_token(args)
        auth_status = "ok"

        task_id = _create_task(args.api_base_url, token, args.tool_id)
        _, cstatus = _wait_for_runtime(args.api_base_url, token, task_id, timeout_s=args.startup_timeout)
        container_status = str(cstatus.get("status", "unknown"))

        params, schema = _build_safe_params(args.tool_id, full=(getattr(args, "params", "minimal") == "full"))
        result = asyncio.run(_execute_via_filecomm(task_id, args.tool_id, params, timeout_s=args.exec_timeout))

        stderr = str(result.get("stderr", ""))
        metadata = result.get("metadata", {}) if isinstance(result.get("metadata"), dict) else {}
        validation_failed = stderr.startswith("Validation error:") or metadata.get("error_type") == "validation_error"
        if validation_failed:
            verdict = "FAIL"
            reason = "Schema validation failed at runtime"
        else:
            verdict = "PASS"
            reason = "Tool executed through real Kali runtime path"

    except Exception as exc:
        verdict = "FAIL"
        reason = str(exc)
    finally:
        _write_report(
            report_path=report_path,
            tool_id=args.tool_id,
            auth_method=auth_method,
            auth_status=auth_status,
            task_id=task_id,
            container_status=container_status,
            schema=schema,
            params=params,
            result=result,
            verdict=verdict,
            reason=reason,
        )
        if task_id is not None and token:
            if verdict == "PASS" or not args.keep_on_failure:
                try:
                    _cleanup_task(args.api_base_url, token, task_id)
                except Exception:
                    pass

    print(f"[kalitool] verdict={verdict} report={report_path}")
    return 0 if verdict == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
