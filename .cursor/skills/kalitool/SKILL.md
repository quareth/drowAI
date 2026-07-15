---
name: kalitool
description: Runs a user-specified tool against a real Kali task container using the tool's real schema and safe placeholder targets, then writes a markdown validation report. Use when the user asks to test a tool in real Kali, validate tool parameters from schema, or verify runtime behavior through task/container execution.
---

# Real Kali Tool Schema Test

## Purpose

Validate one user-provided tool end-to-end using:
- real tool schema from registry
- real task/container lifecycle
- real execution path in Kali (no mock fallback)

## Quick Start

Run the utility script (preferred) from the repo root:

```bash
python .cursor/skills/kalitool/scripts/run_real_kali_tool_schema_test.py --tool-id information_gathering.network_discovery.masscan --jwt-token "<JWT>"
```

Or login-based auth:

```bash
python .cursor/skills/kalitool/scripts/run_real_kali_tool_schema_test.py --tool-id information_gathering.network_discovery.nmap --username "<user>" --password "<pass>"
```

## Non-Negotiable Rules

- Use strict real-Kali mode only. If Docker/Kali runtime is unavailable, fail and report.
- Never run against real external targets.
- Use safe placeholders:
  - IP-like fields -> `127.0.0.1`
  - host/domain-like fields -> `example.com`, `localhost`, or `acme.local`
- Require authentication for task APIs via JWT bearer token.
- Output a markdown report file.

## Required Inputs

- `tool_id` (required): exact tool registry id to test.
- Authentication (one required):
  - `jwt_token`, or
  - `username` + `password` (login first to obtain token).

Optional:
- `report_path` (default: `artifacts/tool-schema-test-<tool_id>.md`)
- `keep_on_failure` (default: `false`)
- `api_base_url` (default: `http://localhost:8000`)

## Workflow

Use this checklist and keep it updated while running:

```text
Progress
- [ ] 1) Validate inputs and auth strategy
- [ ] 2) Resolve JWT token
- [ ] 3) Create temporary task
- [ ] 4) Wait for container/runtime readiness
- [ ] 5) Load tool schema from registry
- [ ] 6) Build safe parameters
- [ ] 7) Execute tool via real Kali execution path
- [ ] 8) Collect and evaluate result
- [ ] 9) Write markdown report
- [ ] 10) Cleanup task/container
```

### 1) Validate inputs

- Fail immediately if `tool_id` is missing.
- Fail immediately if neither `jwt_token` nor username/password are present.

### 2) Resolve JWT token

- If `jwt_token` is provided, use it.
- Else call `POST /api/auth/login` and extract `access_token`.
- Use header: `Authorization: Bearer <token>`.
- Never log the raw token.

### 3) Create temporary task

- Call `POST /api/tasks/` with minimal payload.
- Prefer a deterministic name prefix, e.g. `skill-tooltest-<tool_id>-<timestamp>`.
- Track returned `task_id`.

### 4) Wait for readiness

- Poll task/container status until task is active and container exists/runs.
- If startup fails or times out, stop and produce failure report.

### 5) Load real schema

- Fetch schema from tool registry metadata:
  - `get_tool_metadata(tool_id)["args_schema"]`
- Extract required fields and field types.

### 6) Build safe parameters

- Start with minimal valid schema-compliant payload.
- Override target-like fields with safe placeholders.
- Never allow user-provided real targets in this workflow.

Suggested mapping heuristics:
- key contains `ip`, `address`, `src_ip`, `dst_ip` -> `127.0.0.1`
- key contains `target`, `host`, `hostname`, `domain`, `url`:
  - if field appears IP-typed -> `127.0.0.1`
  - else -> `example.com` (or `http://localhost` if URL with scheme is required)

### 7) Execute in real Kali

- Use the real task/container execution path (the same runtime route used by the system).
- Do not substitute mock execution.
- Capture:
  - success
  - exit_code
  - stdout/stderr
  - metadata
  - validation errors (if present)

### 8) Evaluate

- PASS when schema validation succeeds and command execution reaches a valid tool result.
- FAIL when any of:
  - auth failure
  - task/container startup failure
  - schema validation error
  - runtime execution error
  - timeout

### 9) Write markdown report

Use this template:

```markdown
# Tool Schema Runtime Test Report

## Tool
- tool_id: `<tool_id>`
- mode: `strict-real-kali`

## Authentication
- method: `jwt` | `login`
- status: `ok` | `failed`

## Runtime
- task_id: `<task_id>`
- container_status: `<status>`

## Schema Summary
- required_fields: `<count>`
- optional_fields: `<count>`

## Parameters Used
```json
{ ...safe_params... }
```

## Execution Result
- success: `<true|false>`
- exit_code: `<code>`

### stdout (excerpt)
```text
...
```

### stderr (excerpt)
```text
...
```

## Verdict
- status: `PASS` | `FAIL`
- reason: `<short reason>`
```

### 10) Cleanup

- Default behavior: stop/remove runtime and delete temporary task.
- If `keep_on_failure=true`, preserve task for debugging and mention this in the report.

## Failure Policy

- Do not silently downgrade to mock or host-only execution.
- Do not continue after auth failure.
- Do not continue when safe-target enforcement cannot be guaranteed.

## Trigger Examples

- "Test `information_gathering.network_discovery.masscan` with real schema in Kali."
- "Validate this tool using real task container flow."
- "Run tool schema + runtime check and give me a markdown report."

## Utility Scripts

- `scripts/run_real_kali_tool_schema_test.py`: executes the full flow (auth, task/container, schema-safe params, FileComm run, report, cleanup).
- `scripts/generate_kalitool_tool_state.py`: generates `artifacts/kalitool-tool-state.md` from the tool registry (categories and tools). Used by the **kalitool-batch-tester** subagent to get the list to test; the subagent marks each tool completed in that file.
