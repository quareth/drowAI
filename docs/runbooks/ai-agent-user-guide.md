# DrowAI — AI Agent User Guide

A walkthrough of the DrowAI web interface for end users: how to get into the app, set it up, organize work into engagements and tasks, talk to the agent, control how autonomously it acts, and find your way around the rest of the platform.

---

## Table of Contents

1. [Signing in and the main layout](#1-signing-in-and-the-main-layout)
2. [Settings — API keys, CVE database, and more](#2-settings--api-keys-cve-database-and-more)
3. [Engagements — grouping work by client/project](#3-engagements--grouping-work-by-clientproject)
4. [Tasks — creating a pentesting task](#4-tasks--creating-a-pentesting-task)
5. [The agent chat — prompting and watching it work](#5-the-agent-chat--prompting-and-watching-it-work)
6. [Execution modes — Chat / Agent / Agent (Full Access)](#6-execution-modes--chat--agent--agent-full-access)
7. [Plan mode — enabling and disabling](#7-plan-mode--enabling-and-disabling)
8. [Streaming events — what each card means](#8-streaming-events--what-each-card-means)
9. [Approvals and interrupts](#9-approvals-and-interrupts)
10. [Knowledge Workspace — Briefing, Findings, Assets, Evidence, Territory](#10-knowledge-workspace--briefing-findings-assets-evidence-territory)
11. [Reports, Usage, and Profile](#11-reports-usage-and-profile)
12. [Quick reference — common workflows](#12-quick-reference--common-workflows)

---

## 1. Signing in and the main layout

### Logging in

Open the app — any URL other than the login route redirects to `/login` until you authenticate.

1. Enter your username and password.
   - **Default credentials:** username `bot`, password `bot123456`. Change them after the first login from **Profile → Password Change**.
2. Click **Login**.
3. After a successful login, the **Outpost** (home) page loads.

### The main layout

After login, every page shares the same chrome:

#### Top navbar

Across the top of the window:

- **Left** — DrowAI logo and the subtitle *Red Team Platform*.
- **Center** — a search bar with placeholder *Search tasks, logs, or commands…*.
- **Right** — notification bell, a *Credits* counter, and your avatar/username. Clicking the avatar opens a dropdown with:
  - **Profile**
  - **Settings**
  - **Billing**
  - **Logout**

#### Left sidebar (collapsible)

The sidebar is a thin rail by default. Hover it to expand into a drawer. Six destinations, each with an icon:

| Label | Icon | Destination |
|-------|------|-------------|
| **Outpost** | Tent | `/` — dashboard / home |
| **Knowledge** | Brain | `/knowledge` — Knowledge Workspace |
| **Tasks** | List | `/tasks` — task list and chat |
| **Reports** | File | `/reports` — generated reports |
| **Usage** | Gauge | `/usage` — LLM usage and cost |
| **Settings** | Gear | `/settings` — global configuration |

> Tip: hover any rail icon to see its label. The active page shows a blue highlight.

---

## 2. Settings — API keys, CVE database, and more

Open **Settings** from the left sidebar (gear icon) or from the avatar dropdown. The page is organized into five tabs along the top:

| Tab | Purpose |
|-----|---------|
| **API** | API keys (OpenAI, Shodan), AI model selection |
| **Network** | Network/proxy/VPN-related settings |
| **System** | Storage, uptime, data management, danger zone |
| **Display** | UI display preferences |
| **CVE** | Global CVE database indexing |

### 2.1 Setting the OpenAI API key (and other API keys)

1. Click **Settings** (gear icon in the sidebar).
2. Select the **API** tab.
3. Locate the **OpenAI Configuration** card.
4. In the `openai_api_key` field, click the eye icon to show the input, paste your key, and click the eye icon again to mask it. The masked state shows `••••••••`.
5. (Optional) Pick a model from the `openai_model` dropdown.
6. Toggle **`enable_ai`** to **on** to activate AI features.
7. Click **Test OpenAI** to verify the key. A success toast reads *"OpenAI API Test Successful"*; a failure toast tells you what went wrong.
8. Click **Save** to persist the changes.

To configure **Shodan**, scroll down in the same tab to the **Shodan Configuration** card and follow the same paste/save flow with the `shodan_api_key` field.

### 2.2 Configuring the CVE database

The CVE tab controls the global vulnerability index that the agent and findings use to enrich data.

1. Open **Settings → CVE**.
2. **Indexing status** card at the top shows the current state (*Ready*, *Syncing*, *Disabled*, *Needs attention*) along with a colored badge.
3. **Enable CVE indexing** — flip the switch on to start daily syncs.
4. **Daily schedule (UTC)** — pick the hour the automatic sync runs (00:00 UTC … 23:00 UTC). Defaults to `02:00 UTC`.
5. **Manual sync** — click **Sync now** to queue an immediate sync. While a sync is running you can:
   - **Cancel sync** (red outline button) to stop the current run.
   - Watch the live progress card showing the current phase (*Resolving / Downloading / Upserting / Finalizing*) and counters (records processed, inserted, updated).
6. **Last successful sync** — timestamp of the last good run. If a previous run failed, the last error message shows here in red.
7. **Reinstall CVE index** (red danger card at the bottom) — purges all local CVE records and the sync history. Use only when the index is broken or you want a clean baseline. Confirm in the dialog *"Purge CVE index data?"*.
8. **Force purge** — appears only while a sync is running and is genuinely stuck. It cancels the run and purges in one step. Confirm with *"Force purge while sync is running?"*.

### 2.3 System tab

Open **Settings → System** for:

- **Storage**, **Uptime**, **Active Tasks** stats cards.
- **Data Management**:
  - **Auto-cleanup completed tasks** — toggle to remove task data after 30 days.
  - **Backup reports** — toggle to keep report copies.
- **Danger Zone** — **Clear All Data** (red, destructive). Asks for confirmation and wipes the database. Use only on a dev install.

---

## 3. Engagements — grouping work by client/project

Engagements are folders that collect related tasks (e.g. *"Acme — Q2 web app pentest"*). They are optional but recommended for keeping work organized.

### Creating an engagement

You can create an engagement either ahead of time or inline while creating a task. The dialog is the same in both cases:

1. Open the **New engagement** dialog.
2. Fill in **Name** (required).
3. Optionally add **Description (optional)**.
4. Click **Create Engagement**. The new engagement immediately becomes available in the engagement combobox throughout the UI.

### Selecting an engagement on a task

When creating a task you'll see an **Engagement** combobox with placeholder *Search or select…*. Type to search existing engagements, or pick *"Create new engagement"* from the dropdown to open the dialog inline.

### Listing and archiving

The **Knowledge** page groups tasks by engagement. Each row shows the engagement name, the count of tasks under it, and an archive/restore action when no runtime-active tasks belong to it.

> Engagement actions are routed through the Knowledge page in the current build — the legacy `/engagements` URL redirects there.

---

## 4. Tasks — creating a pentesting task

A task is a single piece of work the agent runs. Each task has its own scope, its own isolated container, and its own chat history.

### 4.1 Creating a task

1. Click **Tasks** in the sidebar (or the **Tasks** link from the dashboard).
2. On the Tasks page, click the blue **New Task** button at the top right of the page (icon: plus).
3. The **Create New Pentest Task** modal opens.

Fill in:

| Field | Required? | What to enter |
|-------|-----------|---------------|
| **Task Name** | Yes | A short title — e.g. *Web Application Assessment*. |
| **Target Scope** | Yes | One target per line. Examples: `target.example.com`, `192.168.1.0/24`, `api.client.com`. |
| **Engagement** | Optional | Pick an existing engagement or create a new one inline. |
| **Or Upload Scope File** | Optional | Drag-and-drop or pick a `.md` / `.txt` file. Its contents replace the Target Scope textarea. |
| **Enable VPN** | Optional | Toggle to reveal the VPN configuration form (provider dropdown: `htb`, `tryhackme`, `custom`, plus credentials/config file as needed). |

Click **Create Task** when ready (or **Cancel** to abandon). The task appears on the Tasks page in *pending* state and will transition to *starting* → *running* as the container is spun up.

### 4.2 The Tasks page

Header reads **Tasks** with the subtitle *Manage your pentesting operations*.

- **Search tasks…** — search by name.
- **Filter: All** — dropdown filters by status (All, Pending, Running, Paused, Completed, Failed).
- View toggles — **Grid** vs **List**.
- Stats cards — **Total Tasks**, **Running**, **Completed**, **Failed**.

Each task card shows a name, target summary, a status badge (color-coded — running = green, paused = yellow, completed = gray, failed = red), and a `…` overflow menu with:

- **Container Status** / **Hide Monitor**
- **Refresh**
- **View Details**
- **Download Logs**
- **Delete Task** (red)

Inline action buttons depend on the status:

- **Stop** — running tasks
- **Resume** — paused tasks
- **Retry** — failed tasks
- **Terminal** — opens the task's terminal in a side panel

### 4.3 Task statuses

`pending` → `queued` → `starting` → `running` ⇄ (`paused`, `pausing`, `resuming`) → `stopping` → `completed` / `failed`. A task can also enter `waiting_for_human` when it stops mid-run for an approval.

---

## 5. The agent chat — prompting and watching it work

The chat panel is where you actually talk to the agent. It opens when you click a task on the Tasks page and lives in the right side of the task workspace.

### 5.1 Sending a prompt

1. Open a task by clicking it from the Tasks list.
2. The chat panel loads on the right; any prior conversation history is restored.
3. At the bottom of the panel, find the **chat input** — a textarea with a placeholder. Above the textarea is a controls row:
   - **Mode dropdown** (left) — currently selected execution mode (Chat / Agent / Agent (Full Access)).
   - **Plan toggle** (next to it) — clipboard icon plus the label *Plan*.
   - **Send button** (right) — up-arrow icon.
4. Type your instructions. *"Run a port scan on the target and tell me what's open."*
5. Press **Enter** or click the send button.
6. The chat starts streaming. See [§8](#8-streaming-events--what-each-card-means) for what each event card means.

### 5.2 What you see while the agent works

Each event the agent emits appears as a card or message bubble in chronological order. Cards expand and collapse, group together (e.g. tool batches), and update in real time as deltas stream in.

A typical sequence in **Agent** mode:

1. Your prompt → **MessageBubble** (user style).
2. The agent's reasoning (if visible) → **ThinkingCard** with header *Thinking…*. When done it collapses to *Thought for Xs*.
3. The agent decides on a tool → **ExecutingToolCard** (*Running: nmap*) — possibly bundled into a **ToolBatchCard** if multiple tools fire in parallel.
4. (Agent mode only) An approval card pops up — see [§9](#9-approvals-and-interrupts).
5. After approval the tool runs and an **ObservingCard** (*Observation*) accumulates output.
6. When the tool finishes the executing card flips to *Completed* (green) or *Failed* (red).
7. The agent writes its response → **MessageBubble** (assistant style), streaming text from `message_start` to `message_section_end`.

---

## 6. Execution modes — Chat / Agent / Agent (Full Access)

The mode dropdown above the input lets you choose how autonomous the agent is. Click the dropdown labeled with the current mode (default *Agent*) to open the menu titled **Switch mode**.

| Option | Label | What happens |
|--------|-------|--------------|
| 💬 | **Chat** | Pure conversation. The agent only chats — it cannot call tools. Plan mode is automatically disabled when this mode is selected. Best for brainstorming or asking questions. |
| 🤖 | **Agent** | The agent reasons and uses tools, but **asks for approval** before each tool call (or batch of tool calls). Best for pentesting where every action should be vetted. |
| 🚀 | **Agent (Full Access)** | The agent runs tools **automatically without asking**. Use only when you trust the scope and the agent's judgment. |

### How to change mode

1. Click the mode pill button on the chat input bar (e.g. *Agent*).
2. The dropdown lists the three modes with descriptions.
3. Click the desired mode. A green check appears next to the selected option and the pill text updates immediately.
4. The next message you send uses the new mode.

> Switching to **Chat** while Plan was on will turn the Plan toggle off and grey it out.

---

## 7. Plan mode — enabling and disabling

Plan mode adds a planning step *before* the agent acts: it produces a goal, a list of steps, and a todo list. You review and approve the plan, then the agent executes against it and reports todo progress as it goes.

### Enabling plan mode

1. Make sure your execution mode is **Agent** or **Agent (Full Access)** (Plan is disabled in Chat mode and shown grey/dim).
2. Click the **Plan** pill button next to the mode dropdown. Active state is emerald green; inactive is slate gray.
3. Send your prompt as normal.

### What you'll see when Plan is on

- A **PlanCard** appears in the conversation showing:
  - **Goal** — one-line objective.
  - **Step 1: …**, **Step 2: …**, etc.
  - **Todo list** with status pills (`pending`, `in_progress`, `completed`, `skipped`).
- Three buttons on the plan: **Approve**, **Edit**, **Reject**.
- After approval, the same card stays at the top of the conversation and updates as todos move from `pending` → `in_progress` → `completed`.

### Disabling plan mode

Click the **Plan** pill again — the green tint fades to slate. The next prompt will skip the planning step.

> If the toggle is greyed out, your current mode is **Chat**. Switch to **Agent** or **Agent (Full Access)** first.

---

## 8. Streaming events — what each card means

The chat is event-streamed. Below is the full set of events you may see, what they look like in the UI, and what they mean.

| Event | Card / UI element | Meaning |
|-------|-------------------|---------|
| `user_message` | User MessageBubble | Your prompt, echoed back. |
| `message_start` / `message_delta` / `message_section_end` | Assistant MessageBubble | The agent's text reply, streaming in. |
| `reasoning_start` / `reasoning_delta` / `reasoning_section_end` | **ThinkingCard** | The agent is "thinking". Header reads *Thinking…* while live, and *Thought for Xs* when complete. Click to expand and read the reasoning. |
| `tool_start` / `tool_delta` / `tool_end` | **ExecutingToolCard** | A single tool call. Shows the tool name and a status: *Running* → *Completed* (green) / *Failed* (red). |
| `tool_batch_start` / `tool_batch_end` | **ToolBatchCard** | Multiple tools were dispatched together. Each child tool renders inside the batch card. |
| `observation_start` / `observation_delta` / `observation_section_end` | **ObservingCard** | The output of a tool — stdout/stderr/observation text — streaming as it is produced. |
| `retry_start` / `retry_attempt` | **RetryCard** | A tool call is being retried after a transient failure. Shows attempt N of M. |
| `graph_interrupt` | **ToolApprovalCard** / **PlanCard** / **ClarifyRequiredCard** | The agent has paused and is waiting on you. See [§9](#9-approvals-and-interrupts). |
| `plan_created` | **PlanCard** | The agent finished planning. Plan + todos are displayed. |
| `todo_progress` | **PlanCard update** | A todo item changed status. The card updates in place. |
| `agent_pause_request` | **PauseCard** | The agent has paused itself (e.g. mid-run). You can resume manually. |
| `intent_summary` | Intent card | A high-level summary of what the agent is about to do. |
| `status` | Status indicator | Connection / processing status (rare; usually transient). |
| `stream_error` | Red error MessageBubble | An error occurred in the agent or backend. The text explains what failed. |

> Cards are collapsible. Click the header of a ThinkingCard, ToolBatchCard, or ObservingCard to expand or collapse it.

---

## 9. Approvals and interrupts

When the agent sends a `graph_interrupt` it is paused and waiting on you. There are three sub-types.

### 9.1 Tool approval (Agent mode)

A **ToolApprovalCard** appears in the chat. It shows:

- The **tool name** and a short description.
- (When set) a **risk level** badge — `Low` (emerald), `Medium` (amber), or `High` (rose).
- The **parameters** the agent wants to use, rendered as JSON. You can edit this JSON in place.
- Three buttons:
  - **Approve** (green) — runs the tool with the parameters as shown.
  - **Edit** (pencil) — opens an inline editor for the parameters; *Approve* runs them as edited.
  - **Skip** (×) — refuses this call. The agent receives a "skipped" signal and may try something else.

For batch approvals (multiple tools) the card switches to a list — each row has its own per-row controls and the card has a single **Submit** button to send all decisions at once.

### 9.2 Plan review

A **PlanCard** appears with **Approve**, **Edit**, **Reject** buttons. Editing rewrites the plan inline; *Reject* cancels the planning attempt and the agent re-plans or asks for more direction.

### 9.3 Clarification request

A **Clarification Required** card appears when the agent can't proceed without more info. It renders the agent's questions as labeled inputs (text fields or dropdowns). Fill them in and click **Submit**.

> While any interrupt is open, the chat input is still usable but the agent will not act until the interrupt is resolved.

---

## 10. Knowledge Workspace — Briefing, Findings, Assets, Evidence, Territory

The **Knowledge** page (`/knowledge`) is the canonical store of what the agent has learned. Open it from the sidebar (brain icon). Across the top sit five tabs:

| Tab | Icon | What's inside |
|-----|------|---------------|
| **Briefing** | Scroll | High-level summary cards covering scope, status, and the latest activity. Start here for an overview. |
| **Findings** | File-search | Security findings discovered during runs. Filter by severity (Critical, High, Medium, Low, Info), status, exploited (true/false), source, asset, and free-text query. |
| **Assets** | Globe | Hosts, services, URLs, accounts, and other assets. Filter by type, vulnerable (yes/no), exploited (yes/no), and free-text query. |
| **Evidence** | Archive | Raw evidence — captured outputs, screenshots, files. Filter by type, observed date, source, and free-text query. |
| **Territory** | Map | Relationship graph / topology map. Requires an active engagement to be selected (the helper text reads *"Territory scope requires an engagement."*). |

### Switching engagements

The Knowledge page has an engagement selector at the top. Picking an engagement scopes Findings / Assets / Evidence / Territory to that engagement. Tasks listed in Briefing follow the same scope.

### Drilling into items

Click any row (a finding, asset, or evidence item) to open its detail drawer with full provenance — when it was first observed, which task / tool produced it, and the underlying evidence chain.

---

## 11. Reports, Usage, and Profile

### 11.1 Reports

Open **Reports** (file icon, sidebar). The page header reads **Reports**.

- Use **Search reports…** to find by name.
- **Filter: all** dropdown filters by report format.
- Each report card shows:
  - Title (e.g. *Task #42 Report*)
  - Generated timestamp
  - Severity badge — *Clean*, *Low Risk*, *Medium Risk*, *High Risk*
  - Finding count
  - Action buttons: **View**, **Download**, **Share**

### 11.2 Usage

Open **Usage** (gauge icon, sidebar). The header reads **Usage** with the subtitle *Per-task LLM usage, cache behavior, and cost breakdown.*

1. At the top, the **Task** dropdown selects which task's usage to view.
2. The panel below shows:
   - Overview cards — tokens used, cost, cache hits.
   - Group breakdown chart — by provider, model, role.
   - Timeline chart — usage over time.
   - Per-call records table — every LLM call with timestamp, model, token counts, and cost.

### 11.3 Profile

Open from the avatar dropdown → **Profile** (or `/profile`). The header reads **User Profile**. The page shows:

- Avatar and username.
- Stats — *Tasks Completed*, *Vulnerabilities Found*, *Critical Findings*, *Success Rate*, plus *Rank*, *Level*, *Experience*.
- A **Recent activity** list with timestamps.
- A **Password Change** form for updating credentials.
- A **Back to Dashboard** button.

---

## 12. Quick reference — common workflows

### Run my first scan

1. **Settings → API** — paste OpenAI key, toggle **enable_ai** on, click **Save**.
2. **Tasks → New Task** — name it, paste the target into **Target Scope**, click **Create Task**.
3. Open the new task. The chat panel loads.
4. Verify the mode pill says **Agent** and the **Plan** toggle is off.
5. Type *"Enumerate the target and report open ports and services."* and press Enter.
6. Approve each tool call as the **ToolApprovalCard** appears.
7. When the agent finishes, open **Knowledge → Findings** to see what was discovered.

### Let the agent run autonomously

1. In a task chat, click the mode pill and switch to **Agent (Full Access)**.
2. (Optional) Turn **Plan** on to require an approved plan up front.
3. Send the prompt — the agent will use tools without per-call approval.

### Refresh the CVE database

1. **Settings → CVE**.
2. Click **Sync now**. Watch the progress card.
3. If it gets stuck, click **Cancel sync**, then **Purge index data** if a clean rebuild is needed.

### Find a specific finding

1. **Knowledge → Findings**.
2. Use the severity / status / asset filters at the top, or type into the search box.
3. Click the row to see provenance — which task and tool produced it.

### Stop a runaway task

1. **Tasks**.
2. Find the task card.
3. Click **Stop**. The status moves to *stopping* → *paused* (resumable) or *failed* depending on the cause.

---

*End of guide.*
