<!-- Purpose: record the validated, behavior-preserving retirement of inert shell command-length configuration plumbing. -->

# Retire the Inert Shell Command-Length Configuration

Status: Implemented and verified (2026-08-07)

## Purpose, scope, and boundaries

Retire the inert `SHELL_EXEC_MAX_COMMAND_CHARS` setting and its internal
forwarding without changing shell validation behavior. The scope is limited to
agent configuration, shared parameter validation, shell policy interfaces,
executor and batch wiring, and directly affected tests. Shell policy rules,
tool schemas, execution, routing, approvals, timeouts, and output limits remain
out of scope.

## Reviewed scope and repository evidence

The proposal originated from branch scope
`c4fa5923e5726b261a81542927bc5f1e00b91aed..8465fed63ed3146e4e7804630d532ef4348a8709`.
That diff removed command-length rejection from `agent/tools/shell/policy.py`
while retaining and explicitly discarding `max_command_chars`.

Repository caller tracing confirmed that the obsolete setting remained in
`agent/config.py`, `agent/executor.py`, `agent/tools/parameter_validation.py`,
`agent/graph/subgraphs/tool_execution_runtime/batch_runner.py`, and
`agent/tool_runtime/batch/validator.py`. The batch-validator hop was part of the
wired path and therefore had to be retired atomically with the originally
identified configuration and shell-policy interfaces.

## Maintainability problem

The repository exposed and transported a configuration value that no longer
affected shell validation. This created false authority: callers and operators
could reasonably expect changing `SHELL_EXEC_MAX_COMMAND_CHARS` to change
runtime behavior while the owning validator intentionally ignored it.

## Validated symbol and change inventory

- `AgentConfig.shell_exec_max_command_chars` and its environment parsing:
  removed.
- `EnhancedCommandExecutor._validate_tool_parameters`: stopped forwarding the
  setting.
- `validate_tool_parameters` and its planner/execution helpers: removed the
  inert parameter and forwarding.
- `validate_shell_tool_parameters` and `validate_shell_exec_command`: removed
  the inert parameter.
- `batch_runner.validate_batch` and `BatchValidator._normalize_parameters`:
  removed the unused context value and forwarding.
- Configuration and batch-runner tests: revised only where they represented the
  retired contract.

No new module, owner, dependency edge, fallback, compatibility alias, or feature
flag was needed. Shell policy remains the canonical owner of the checks it
actually performs; schema models remain the canonical owner of structural
argument bounds.

## Behavior-preservation constraints

- Preserve the behavior that command length alone does not reject `shell.exec`
  or `shell.script` requests.
- Preserve all policy checks for command segments, wrapper payloads, pipelines,
  and obvious hazards.
- Preserve validation error shapes, metrics for real policy rejections, tool
  schemas, routing, and runtime results.
- Do not introduce a replacement length limit or reinterpret another timeout or
  output setting as a command limit.

## Implementation and verification phases

### Phase 1 — Baseline and repository validation

- Traced the wired configuration, executor, shared parameter-validation, shell
  policy, batch-runner, and batch-validator paths.
- Locked the focused pre-change baseline at 68 passing tests, including explicit
  long-command acceptance and existing shell policy rejection coverage.

### Phase 2 — Atomic retirement

- Removed the configuration field, environment parsing, function parameters,
  and all wired forwarding in one focused change.
- Preserved all validator bodies and policy behavior apart from removal of the
  already ignored arguments.

### Phase 3 — Review & Cleanup

- Re-ran the identical focused suite: 68 passed, matching the baseline.
- Confirmed the retired names remain only in documentation and the regression
  test that proves the old environment variable is ignored; no runtime
  validation or configuration path references them.
- Confirmed no duplicate owner, fallback, compatibility shim, new flag, unused
  import, or new module was introduced.
- Confirmed the diff passes whitespace validation.

## Non-goals

- Changing shell command policy or the set of blocked commands.
- Changing command execution, routing, approval, timeout, or output limits.
- Adding a new configuration mechanism or compatibility alias.
- Refactoring unrelated agent configuration fields.
