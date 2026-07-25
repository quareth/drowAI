---
name: drowai-tool-mechanical-validation
description: Mechanically validate one implemented DrowAI tool through its real Kali schema path and current web UI. Use after implementation review to verify parameters, execution/result states, deterministic compression, artifacts, knowledge, model/tool selection, and cleanup without judging prompt prose or answer quality.
---

# DrowAI Tool Mechanical Validation

Validate mechanics only. A fluent answer cannot rescue broken parameters or
results, and awkward prose is never a failure when the mechanical evidence is
correct.

## Required inputs

- Exact visible `tool_id` and reviewed implementation guide.
- Completed capability-analysis state.
- Minimal and full schema profiles.
- A loopback, reserved, or locally controlled fixture profile.
- An existing verified NVIDIA connection. Never create, reveal, copy, or log
  its key.

Initialize `.codex/agents/drowai-tool-mechanical-validation-state.md` from its
committed example. The live state and reports under `artifacts/` are ignored and
must never be staged.

## Safety gate

- Default IP/URL targets are loopback.
- Default domain target is the RFC-reserved `drowai.test`; never use
  `example.com` or an inferred public/private third-party target.
- A DNS success fixture must pair a reserved test domain with a locally
  controlled resolver. If that fixture does not exist, record the success case
  as blocked and stop with `NEEDS_CLARIFICATION`; do not substitute a live
  domain.
- Intrusive tools run only against disposable local fixtures designed for that
  exact mechanic.
- Missing schema, parameter loss, unsafe targeting, false-positive success,
  runtime failure, or unknown cleanup is a mechanical failure.
- Mask credential state only as `<KEY_SET>` or `<NO_KEY>`.

## Workflow

### 1. Preflight and stack ownership

1. Read `AGENTS.md`, `docs/runbooks/ai-agent-user-guide.md`, and
   `docs/runbooks/browser-testing-scenarios.md`, then verify paths/labels against
   current code and a fresh browser snapshot.
2. Check `http://127.0.0.1:8000/api/health`.
3. Start `python3 scripts/local_dev.py up` only when the stack is not healthy.
   Record `stack.started_by_workflow: true`. If it was already healthy, record
   false.
4. Stop the stack at the end only when this workflow started it.

### 2. Direct real-Kali mechanics

Use the existing `kalitool` skill; do not recreate task/runtime dispatch:

```bash
.venv/bin/python .codex/skills/kalitool/scripts/run_real_kali_tool_schema_test.py \
  --tool-id <exact-tool-id> --params minimal --jwt-token "<TOKEN>"
.venv/bin/python .codex/skills/kalitool/scripts/run_real_kali_tool_schema_test.py \
  --tool-id <exact-tool-id> --params full --jwt-token "<TOKEN>"
```

Pass credentials through the process only and never paste them into state,
reports, commentary, or Git. Confirm generated targets are loopback,
`localhost`, `http://localhost`, or `drowai.test` before execution.

### 3. Result scenarios

Mechanically prove the applicable cases with real local execution or
representative parser/runtime fixtures:

- successful non-empty result;
- valid empty result;
- partial or timeout result;
- actual failed execution.

For each, verify truthful success/status/exit classification, stable result
shape, artifact behavior, semantic/knowledge behavior, and absence of
false-positive success. Fixture-only cases must be labeled as such.

### 4. Deterministic compression

Use small and over-budget representative results. Verify exact
`total`, `shown`, and `omitted` values, including the omission marker inside
the same item/character budget. Require reordered equivalent input to produce
the same normalized compact result. The authority is
`agent/graph/compression/deterministic/budget.py::budget_rendered_items`.

### 5. Current GUI mechanics

Use the existing `playwright` skill and its snapshot-first rules.

1. Log in through the current app UI using the configured local test account.
2. Create a task from the current task panel; leave scope/VPN fields blank
   unless the controlled fixture requires them.
3. Wait for task/chat readiness.
4. In the model selector choose **Open models** → **GPT-OSS 20B** →
   **NVIDIA**. Confirm the selected deployment resolves to:
   - provider preset `nvidia_nim_openai_compatible_chat`
   - model `openai/gpt-oss-20b`
5. Use **Agent (Full Access)** only with the reserved/local target profile.
6. Send bounded mechanical prompts for minimal and full parameters. Record the
   selected tool ID, transmitted parameters, rendered tool result, artifacts,
   and knowledge updates.

If the model selects another tool or never calls the selected tool, retry with
an explicit tool-name/schema prompt at most twice. Then classify
`INCONCLUSIVE_MODEL_SELECTION`; do not call the tool broken. A missing catalog
entry/function spec or selected tool that loses parameters is a failure.

### 6. Record and clean up

Record a secret-safe result using
[references/mechanical-scorecard.md](references/mechanical-scorecard.md) and
update the mechanical-validation state. The reviewer judges the recorded
evidence directly; do not add a report validator or workflow test suite.

Delete the temporary validation task/runtime. Preserve a failed task only when
the user explicitly requests debugging; otherwise cleanup failure routes to
`NEEDS_CLEANUP`.

## Final statuses

- `PASS`: all required mechanics pass.
- `FAIL`: a tool/schema/runtime/result/compression/artifact/knowledge mechanic
  fails.
- `INCONCLUSIVE`: only bounded model-selection uncertainty remains.
- `NEEDS_CLEANUP`: workflow-created runtime/task/stack state was not cleaned.
- `NEEDS_CLARIFICATION`: a controlled fixture, verified connection, or required
  scope decision is missing.

Do not score prose quality, prompt quality, helpfulness, tone, or answer style.

## Validation

```bash
python3 /Users/gunesalcan/.codex/skills/.system/skill-creator/scripts/quick_validate.py \
  .codex/skills/drowai-tool-mechanical-validation
```
