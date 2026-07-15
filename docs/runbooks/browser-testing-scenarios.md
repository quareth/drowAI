# Browser Testing Runbook — Agent Chat Scenarios

Manual QA runbook for exercising the chat UI in a real browser. Each scenario sets up a specific mode + plan-toggle combination, sends a fixed prompt, and lists the events / cards / interactions you should observe.

Use these scenarios to:

- Smoke-test the agent end-to-end after a deploy.
- Verify mode and plan-toggle behavior didn't regress.
- Confirm streaming events render correctly (Thinking, Tool, Observation, Approval, Plan, Message).
- Validate Knowledge / Findings / Assets are populated as the agent runs.

> Cross-reference: see `docs/runbooks/ai-agent-user-guide.md` for the overall UI tour and definitions of the cards mentioned below.

---

## Pre-flight (do once before running any scenario)

1. Start the stack and the dev server.
2. Open the app in Chrome / Firefox.
3. Log in with the default credentials — username `bot`, password `bot123456`.
4. **Settings → API**:
   - `openai_api_key` is set.
   - `enable_ai` is **on**.
   - Click **Test OpenAI** → toast reads *"OpenAI API Test Successful"*.
5. **Tasks → New Task**:
   - **Task Name:** `Browser QA — <scenario letter>` *(this is the only field you set)*.
   - Click **Create Task** immediately. **Do NOT** fill in Target Scope, Engagement, scope file, VPN, or any other field.
6. Open the task. Wait until the status badge turns **running** (container ready). The chat panel becomes interactive.
7. (Optional) Open the browser DevTools **Network** tab and filter `EventSource` / `WebSocket` to inspect the live stream while testing.

### Task-creation rule (applies to every scenario)

> **Only set `Task Name` when creating a task. Do NOT set any other field — Target Scope, Engagement, scope file upload, VPN, or anything else — unless the scenario step explicitly says to.** This keeps every test run starting from the same default-config baseline. None of the scenarios in this runbook ask you to set additional fields, so leave them all blank.

### Pass / fail conventions used below

- ✅ = expected — must be observed for the scenario to pass.
- ❌ = must NOT happen — a regression if it does.
- ⚠️ = acceptable variation that does not fail the scenario.

---

## Scenario 1 — Agent + Plan ON: Docker network sweep + Postgres check

### Goal
Verify the planning + per-tool approval flow works for a multi-step recon objective.

### Setup

1. In the chat input row, click the **mode pill** and select **Agent**.
2. Click the **Plan** pill so it turns **emerald green** (active).
3. Confirm both indicators visually before sending the prompt.

### Prompt

Paste verbatim into the chat input and press Enter:

```
Scan current docker network /24 and find online hosts if any host online scan it for Postgre port.
```

### Expected sequence in the chat

1. ✅ User MessageBubble echoes the prompt.
2. ✅ A **ThinkingCard** appears (header *Thinking…*) and streams reasoning. Collapses to *Thought for Xs* on completion.
3. ✅ A **PlanCard** appears containing:
   - A **Goal** referencing the docker /24 sweep + Postgres check.
   - At least 2 numbered steps (e.g. *Step 1: Identify the docker network*, *Step 2: Sweep online hosts*, *Step 3: Probe Postgres port 5432*).
   - A todo list with at least one `pending` item.
   - **Approve / Edit / Reject** buttons.
4. Click **Approve** on the plan.
5. ✅ The first **ToolApprovalCard** opens for a discovery action (e.g. `ip route` / `ifconfig` / `nmap -sn <cidr>`):
   - Tool name visible.
   - Parameters block visible and editable.
   - Risk badge shown if the tool is tagged.
   - Buttons: **Approve**, **Edit**, **Skip**.
6. Click **Approve**. The card flips to **ExecutingToolCard** → *Running* → *Completed* (green) and an **ObservingCard** streams the output.
7. ✅ The PlanCard updates — the matching todo flips to `in_progress`, then `completed`.
8. ✅ Subsequent **ToolApprovalCard(s)** appear for each follow-up tool the agent wants to run (host enumeration, Postgres probe with `nmap -p 5432`, etc.). Each one waits for **Approve / Edit / Skip**.
9. ✅ When the Postgres probe runs, the ObservingCard contains either a port-state line (`5432/tcp open|closed|filtered`) or a clear "no online hosts" finding.
10. ✅ Final assistant **MessageBubble** summarizes:
    - Hosts found online (or that none were).
    - Postgres reachability per host.
11. ✅ **Knowledge → Assets** lists discovered hosts as new assets when applicable.
12. ✅ **Knowledge → Evidence** has at least one new evidence row (the scan output).

### Negative checks

- ❌ Tools must NOT execute before you click **Approve** (Agent mode requires HITL).
- ❌ Plan must NOT auto-approve — the buttons must remain interactive until you click one.
- ❌ No `stream_error` red bubbles unless a tool legitimately failed (in which case the agent should retry or report cleanly).

### Edit-path variant (optional)

On any ToolApprovalCard, click **Edit**, change a flag (e.g. add `-Pn` to nmap), then **Approve**. The ObservingCard output should reflect the edited command (look for the modified flag in the rendered command line).

---

## Scenario 2 — Agent + Plan OFF: Docker network sweep + Postgres check

### Goal
Verify plain Agent mode (per-tool approval, no upfront plan) works for the same objective.

### Setup

1. Mode pill → **Agent**.
2. Plan pill → **off** (slate gray, not emerald).
3. Verify both indicators before sending.

### Prompt

```
Scan current docker network /24 and find online hosts if any host online scan it for Postgre port.
```

### Expected sequence

1. ✅ User MessageBubble.
2. ✅ ThinkingCard streams.
3. ❌ **No PlanCard** is rendered for this run (the plan toggle is off).
4. ✅ The first **ToolApprovalCard** appears immediately for a discovery action.
5. Click **Approve**. ExecutingToolCard → ObservingCard with output.
6. ✅ Successive ToolApprovalCards appear for each tool the agent picks (network sweep, then Postgres probe).
7. ✅ Final MessageBubble summarizes hosts + Postgres reachability.
8. ✅ Knowledge tabs (Assets / Evidence) reflect discovered data.

### Negative checks

- ❌ No PlanCard.
- ❌ No tool runs without your **Approve** click.
- ❌ The Plan pill must NOT silently re-enable itself.

### Comparison vs. Scenario 1

The only structural difference from Scenario 1 should be the absence of the upfront PlanCard and the absence of `todo_progress` updates. The number and content of approval cards should be roughly equivalent.

---

## Scenario 3 — Agent (Full Access) + Plan OFF: Single-port loopback probe

### Goal
Verify Full-Access mode runs tools without per-call approval.

### Setup

1. Mode pill → **Agent (Full Access)**.
2. Plan pill → **off** (it can be toggled but leave it off).
3. Verify the pill text reads exactly **Agent (Full Access)** before sending.

### Prompt

```
Scan 127.0.0.1 for postgre port.
```

### Expected sequence

1. ✅ User MessageBubble.
2. ✅ ThinkingCard streams (may be brief — the task is small).
3. ✅ An **ExecutingToolCard** appears directly (e.g. `nmap -p 5432 127.0.0.1`).
4. ❌ **No ToolApprovalCard appears.** The tool runs immediately.
5. ✅ ObservingCard streams scan output. Look for a port-state line for `5432/tcp`.
6. ✅ Tool card flips to *Completed* (green). If Postgres isn't running locally, the tool still completes — the *port closed/filtered* result is a valid pass.
7. ✅ Final MessageBubble explicitly states whether port `5432` is **open**, **closed**, or **filtered**.
8. ✅ The `127.0.0.1` asset (or its existing record) gains evidence linked to this run in **Knowledge → Evidence**.

### Negative checks

- ❌ No ToolApprovalCard. If one appears, the mode is not actually `agent_full` — check the pill and the network panel for the request payload.
- ❌ No PlanCard.

### Quick perf check

Time from prompt-submit to first ObservingCard delta should be a few seconds, not tens of seconds. If it stalls, check the streaming hub and the kali_executor logs.

---

## Scenario 4 — Agent (Full Access) + Plan OFF: Open-ended next-step suggestion

### Goal
Verify the agent answers a context-sensitive "what next?" question using prior task context, **without** firing tools when none are needed.

### Setup

1. **Run Scenario 3 first** in the same task so there is prior context (a completed Postgres probe on `127.0.0.1`).
2. Confirm mode pill is still **Agent (Full Access)** and Plan pill is **off**.

### Prompt

```
What you you suggest for next step.
```

(Yes — typo intact. The agent should still understand it.)

### Expected sequence

1. ✅ User MessageBubble.
2. ✅ ThinkingCard streams (the agent considers the prior tool output).
3. ⚠️ Optional: 0–2 lightweight read-only tool calls (e.g. re-reading scope, checking knowledge). These are fine in Full Access mode.
4. ❌ No invasive scan should fire automatically without the user's prompting (a follow-up scan is acceptable only if the agent treats it as part of the suggestion plan, but even then it should run *after* answering).
5. ✅ Final MessageBubble lists 2–5 concrete suggestions, ideally referencing what was just learned, e.g.:
   - *"Postgres is open on 127.0.0.1 — try a banner / version probe and weak-credential check."*
   - *"Postgres is closed — pivot to other ports (22, 80, 443) or other discovered hosts."*
   - *"Capture evidence in the Findings tab and proceed to the next host in scope."*
6. ✅ Suggestions are grounded in this task's actual prior output, not generic boilerplate.

### Negative checks

- ❌ A bare *"I'm not sure, please clarify."* answer that ignores prior context is a fail.
- ❌ No `stream_error` bubbles.
- ❌ The agent should not say it has no tools / no context when a Postgres probe just completed in the same conversation.

## Scenario 5 — Anthropic Haiku + Agent (Full Access) + Plan OFF: Single-port loopback probe

### Goal
Verify Full-Access mode runs tools without per-call approval while the selected chat model is **Anthropic / Claude Haiku 4.5**.

### Setup

1. Click the **model selector** above the task selector.
2. Open the **Anthropic** provider submenu.
3. Select **Claude Haiku 4.5**.
4. Confirm the model selector reads **Anthropic / Claude Haiku 4.5**.
5. Mode pill → **Agent (Full Access)**.
6. Plan pill → **off** (it can be toggled but leave it off).
7. Verify the pill text reads exactly **Agent (Full Access)** before sending.

### Prompt

```
Scan 127.0.0.1 for postgre port.
```

### Expected sequence

1. ✅ User MessageBubble.
2. ✅ ThinkingCard streams (may be brief — the task is small).
3. ✅ An **ExecutingToolCard** appears directly (e.g. `nmap -p 5432 127.0.0.1`).
4. ❌ **No ToolApprovalCard appears.** The tool runs immediately.
5. ✅ ObservingCard streams scan output. Look for a port-state line for `5432/tcp`.
6. ✅ Tool card flips to *Completed* (green). If Postgres isn't running locally, the tool still completes — the *port closed/filtered* result is a valid pass.
7. ✅ Final MessageBubble explicitly states whether port `5432` is **open**, **closed**, or **filtered**.
8. ✅ The `127.0.0.1` asset (or its existing record) gains evidence linked to this run in **Knowledge → Evidence**.
9. ✅ **Usage** for the task records the call under provider **Anthropic** and model **Claude Haiku 4.5** / `claude-haiku-4-5-20251001`.

### Negative checks

- ❌ No ToolApprovalCard. If one appears, the mode is not actually `agent_full` — check the pill and the network panel for the request payload.
- ❌ No PlanCard.
- ❌ The model selector must NOT silently revert to an OpenAI model before or during the run.
- ❌ No Anthropic credential/configuration error should appear. If it does, Settings → API is missing a working Anthropic key.

### Quick perf check

Time from prompt-submit to first ObservingCard delta should be a few seconds, not tens of seconds. If it stalls, check the streaming hub and the kali_executor logs.

---

## Cross-scenario regression checklist

Tick these once after running all five scenarios in order:

- [ ] Mode pill state persists across page refresh within the same task.
- [ ] Plan pill is correctly **disabled and dim** when mode is `Chat` (switch to Chat briefly to verify, then back).
- [ ] Switching to **Chat** mid-task with Plan ON automatically clears the Plan toggle.
- [ ] No console errors in the browser DevTools console after running all five scenarios.
- [ ] WebSocket / SSE connection in DevTools Network tab stays open across the entire test session (no repeated reconnects).
- [ ] Task status remained **running** throughout all five scenarios; no unexpected `failed` / `paused`.
- [ ] After completion, **Knowledge → Briefing** for the engagement reflects the runs.
- [ ] **Usage** page for the task shows non-zero token counts and a per-call record list.
- [ ] **Usage** page includes at least one Anthropic / Claude Haiku 4.5 call from Scenario 5.
- [ ] **Reports** page can generate a report for the task without errors (button click → status moves to *generated*, **View** opens it).

---

## Common failure patterns and where to look

| Symptom | Likely cause | Where to check |
|--------|--------------|----------------|
| ToolApprovalCard appears in Agent (Full Access) mode | mode payload regressed to `agent` | Network panel — outgoing chat request payload `mode` field; `client/src/components/chat/UnifiedAgentChat.tsx` and `ModeSwitcher.tsx`. |
| PlanCard does not appear in Scenario 1 | plan toggle not reaching backend | `client/src/contexts/PlanContext.tsx`; outgoing payload `plan` flag. |
| Stream stalls after first tool | streaming hub backpressure / WS dropped | `backend/services/streaming/in_memory_hub.py`; browser Network → WS frame inspector. |
| Tool `Completed` but no Observing output | tool started but executor returned empty stdout | `kali_executor/executor_daemon.py`; container logs (`docker logs <task-container>`). |
| Knowledge tabs empty after a run | knowledge persistence path broken | `backend/services/knowledge/*`; verify `findings` / `assets` / `evidence` API responses in Network tab. |
| `stream_error` bubble in chat | exception in agent or scope validator | `agent/scope_validator.py`, `agent/executor.py`; backend logs. |

---

## Cleanup

After each test run:

1. Stop the task (task card → **Stop**) so the container is released.
2. (Optional) Delete the QA task via the `…` menu → **Delete Task** to keep the list tidy. Reports and Knowledge will retain their state.

*End of runbook.*
