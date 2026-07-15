# Application Workflows Reference — Interactive Task (One User Query)

This document describes the **full API workflow for a single user query** in **interactive** mode only (no automatic execution). It is intended as a reference for Cursor and developers: each step, endpoint, and payload is listed so the flow can be reproduced or debugged.

---

## Overview: Steps in Order

| Step | Action | Endpoint(s) |
|------|--------|-------------|
| 1 | Login | `POST /api/auth/login` |
| 2 | Task creation | `POST /api/tasks/` |
| 3 | Agent mode (Agent vs Full Access) | Sent per message in chat; no separate endpoint |
| 4 | Task selection | `GET /api/tasks/`, `GET /api/tasks/{task_id}` |
| 5 | Model (LLM) change | `GET /api/llm/selection`, `PUT /api/llm/selection`, `POST /api/llm/tasks/{task_id}/switch` |
| 6 | Sending query | `POST /api/tasks/{task_id}/chat` |
| 7 | Approving tools or plans | `GET /api/tasks/{task_id}/interrupt`, `POST /api/tasks/{task_id}/graph/resume` |

All endpoints under `/api/tasks/` and `/api/llm/` require authentication (Bearer token or `access_token` cookie). Base URL is the backend root (e.g. `http://localhost:8000`).

---

## 1. Login

**Endpoint:** `POST /api/auth/login`

**Request body:**

```json
{
  "username": "string",
  "password": "string"
}
```

**Response:** `200` with body and cookie:

- Body: `{ "access_token": "...", "token_type": "bearer", "expires_in": <seconds>, "user": { "id", "username", "email", ... } }`
- Cookie: `access_token` (httpOnly) is set by the server.

**Usage:** Use the returned `access_token` in subsequent requests as `Authorization: Bearer <access_token>`, or rely on the cookie if the client sends credentials.

**Optional — get current user:** `GET /api/auth/me` returns the profile for the authenticated user.

---

## 2. Task Creation

**Endpoint:** `POST /api/tasks/`

**Request body (TaskCreateVPN):**

- Required: `name` (string), `scope` (string, e.g. `"network"`).
- Optional: `description`, `timeout_seconds`, `max_retries`, `priority`, `mode`, `vpn_enabled`, `vpn_config`.
- For **interactive-only** flow, set **`mode`** to **`"interactive"`** (default is `"automatic"`).

Example:

```json
{
  "name": "Task1",
  "description": "Optional description",
  "scope": "network",
  "mode": "interactive"
}
```

**Response:** `201` with full task object (e.g. `id`, `user_id`, `status`, `mode`, `created_at`, ...).

**Note:** Creating a task bootstraps workspace and queues background init. For interactive chat, the task must be **running** (user starts it via UI; start is `POST /api/tasks/{task_id}/start` in the runtime router).

---

## 3. Agent Mode (Agent vs Full Access)

There is **no dedicated endpoint** for “agent mode”. The mode is sent **per message** in the chat request.

**Modes (backend `AgentMode`):**

| UI / Request value | Backend enum | Behavior |
|--------------------|--------------|----------|
| `agent` | `AgentMode.AGENT` | Asks before each tool execution (HITL tool approval). |
| `agent_full` / `full_access` / `full` | `AgentMode.FULL_ACCESS` | Runs tools automatically; no approval. |
| `plan` | `AgentMode.PLAN` | Plan review + tool approval. |
| `chat` | `AgentMode.CHAT` | Chat only; no tool access. |

For **interactive (no automatic)** behavior, use **`agent`** or **`plan`** in the chat payload (see step 6).

---

## 4. Task Selection

**List tasks:** `GET /api/tasks/`

- Returns all tasks for the current user (e.g. `id`, `name`, `status`, `mode`, ...).
- Frontend uses this list to let the user pick a task.

**Get one task:** `GET /api/tasks/{task_id}`

- Returns the task by `task_id`; 404 if not found or not owned.

**Interactive flow:** User picks a task from the list; the chosen `task_id` is used for chat, LLM switch, interrupt, and resume.

---

## 5. Model (LLM) Change

**Catalog:** `GET /api/llm/models`

- Returns available providers and models (e.g. OpenAI models). No body required.

**Global selection (user default):**  
- `GET /api/llm/selection` — current user default (e.g. `{ "provider": "openai", "model": "gpt-4o-mini" }`).  
- `PUT /api/llm/selection` — set default: body `{ "provider": "openai", "model": "<model_id>" }`.

**Per-task model switch (for a running task):**  
`POST /api/llm/tasks/{task_id}/switch`  
- Body: `{ "model": "<model_id>" }`.  
- Backend persists a control message and signals the task (e.g. container) so the agent uses the new model.  
- Used when the user changes the LLM in the UI for the current task.

---

## 6. Sending a Query (Interactive Chat)

**Endpoint:** `POST /api/tasks/{task_id}/chat`

**Request body (ChatRequest):**

```json
{
  "message": "User's question or instruction",
  "conversation_id": "optional-default-created-if-omitted",
  "stream": true,
  "mode": null,
  "agent_mode": "agent",
  "client_message_id": "optional-client-id"
}
```

- **`message`** (required): The user query. Max length enforced (e.g. 64k); see backend constant.
- **`agent_mode`**: One of `"agent"`, `"plan"`, `"chat"`, `"full_access"` (or `"agent_full"`, `"full"`) to control approval behavior for this turn.
- **`conversation_id`**: Omit to use the default conversation for the task.
- **`stream`**: Typically `true` for SSE streaming.
- **`mode`**: Optional; separate from `agent_mode` in current implementation.
- **`client_message_id`**: Optional client-side id for correlation.

**Response:** `202 Accepted`  
- Body: `{ "success": true, "conversation_id": "<id>" }` or, if queued, `{ "success": true, "conversation_id": "<id>", "queued": true }`.

**Behavior:**

- Backend validates task ownership and, for interactive streaming, may switch the task to **interactive** mode if it was `automatic`.
- User message is stored; a LangGraph generation task is started. Events are delivered via **SSE**: `GET /api/tasks/{task_id}/reasoning/stream` (or the configured reasoning stream endpoint).
- For **interactive** flow, when the graph hits a tool or plan step that requires approval, it **interrupts** and waits for resume (step 7).

**Optional readiness (interactive):**

- `POST /api/tasks/{task_id}/chat/prewarm` — warms per-task chat resources.
- `GET /api/tasks/{task_id}/chat/ready` — returns whether chat can accept sends (`chat_ready`, `task_running`, `checkpointer_ready`, `sse_connected`).

---

## 7. Approving Tools or Plans (HITL)

When the graph is in **agent** or **plan** mode and reaches a tool or plan step, it **interrupts**. The user must fetch the pending interrupt and then resume with an approval (or edit/reject).

### 7.1 Get Pending Interrupt

**Endpoint:** `GET /api/tasks/{task_id}/interrupt`

**Response:**

- **No interrupt:** `{ "has_interrupt": false, "task_id": <task_id> }`.
- **Interrupt present:**  
  `{ "has_interrupt": true, "task_id", "thread_id", "graph_name", "interrupt_type", "payload", "resumable", ... }`

  - **`interrupt_type`:** `"tool_approval"` or `"plan_review"`.
  - **`payload`:** Either a **tool approval** or **plan review** payload (see below).

**Tool approval payload** (when `interrupt_type === "tool_approval"`):

- `type`: `"tool_approval"`
- `tool_id`, `tool_name`, `parameters`, `description`
- Optional: `risk_level`, `estimated_duration`, `turn_sequence`, `turn_id`, `reserved_message_id`

**Plan review payload** (when `interrupt_type === "plan_review"`):

- `type`: `"plan_review"`
- `goal`, `plan_steps` (list of strings), `todo_list` (list of `{ id, text, status }`)
- Optional: `reasoning`, `targets`, `run_id`, `plan_version`, `turn_sequence`, `turn_id`, `reserved_message_id`

The frontend typically polls this endpoint or reacts to an SSE “graph_interrupt” event, then shows a ToolCard or PlanCard for the user to approve/edit/skip/reject.

### 7.2 Resume After Approval

**Endpoint:** `POST /api/tasks/{task_id}/graph/resume`

**Request body (ResumeRequest):**

```json
{
  "interrupt_type": "tool_approval",
  "graph_name": null,
  "response": {
    "action": "approve",
    "edited_parameters": null,
    "edited_goal": null,
    "edited_plan_steps": null,
    "edited_todo_list": null,
    "user_note": null
  }
}
```

- **`interrupt_type`:** Must be `"tool_approval"` or `"plan_review"` to match the current interrupt.
- **`graph_name`:** Optional; if omitted, backend uses stored metadata.
- **`response`** (HITLResumeResponse):
  - **`action`:** `"approve"` | `"edit"` | `"skip"` | `"reject"`.
  - For **tool_approval**: `edited_parameters` (optional), `user_note` (optional).
  - For **plan_review**: `edited_goal`, `edited_plan_steps`, `edited_todo_list`, `user_note` (all optional).

**Response:** Success returns the result of the resume orchestration (e.g. workflow transition and enqueued resume generation). If there is no pending interrupt, backend returns `400` (e.g. “No pending interrupt for this task”).

After a successful resume, the graph continues; new events stream over SSE until the next interrupt or end of turn.

---

## End-to-End Sequence (Interactive, One Query)

1. **Login:** `POST /api/auth/login` → store token / cookie.
2. **Create task (interactive):** `POST /api/tasks/` with `"mode": "interactive"` → get `task_id`.
3. **Start task (if your UI does so):** `POST /api/tasks/{task_id}/start` so the task is running.
4. **Task selection:** `GET /api/tasks/` → user selects `task_id`.
5. **Optional — set/check LLM:**  
   `GET /api/llm/selection`; optionally `PUT /api/llm/selection` or later `POST /api/llm/tasks/{task_id}/switch` with `{ "model": "..." }`.
6. **Optional — chat ready:** `GET /api/tasks/{task_id}/chat/ready` (and optionally prewarm).
7. **Send query:** `POST /api/tasks/{task_id}/chat` with `message` and `agent_mode: "agent"` (or `"plan"`).
8. **Consume stream:** Connect to SSE for the task (e.g. `/api/tasks/{task_id}/reasoning/stream`) to receive assistant and tool events.
9. **When interrupt appears:**  
   - `GET /api/tasks/{task_id}/interrupt` → show ToolCard or PlanCard.  
   - User approves/edits/skips/rejects → `POST /api/tasks/{task_id}/graph/resume` with `interrupt_type` and `response`.
10. Repeat from step 8 until the turn completes (no further interrupt for that message).

---

## Quick Endpoint Reference

| Method | Path | Purpose |
|--------|------|---------|
| POST | `/api/auth/login` | Login |
| GET | `/api/auth/me` | Current user |
| GET | `/api/tasks/` | List tasks |
| POST | `/api/tasks/` | Create task (`mode: "interactive"`) |
| GET | `/api/tasks/{id}` | Get task |
| POST | `/api/tasks/{id}/start` | Start task (runtime) |
| GET | `/api/llm/models` | List models |
| GET | `/api/llm/selection` | Get LLM selection |
| PUT | `/api/llm/selection` | Set LLM selection |
| POST | `/api/llm/tasks/{id}/switch` | Switch task model |
| GET | `/api/tasks/{id}/chat/ready` | Chat ready check |
| POST | `/api/tasks/{id}/chat/prewarm` | Prewarm chat |
| POST | `/api/tasks/{id}/chat` | Send message (query) |
| GET | `/api/tasks/{id}/interrupt` | Get pending HITL interrupt |
| POST | `/api/tasks/{id}/graph/resume` | Resume after tool/plan approval |

This reference covers **interactive** usage only; automatic mode and non-chat flows are out of scope.
