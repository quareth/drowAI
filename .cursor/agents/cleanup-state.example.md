# cleanup-state.example.md

Template for `.cursor/agents/cleanup-state.md`. Copy the YAML block into that file between `---` delimiters when starting a new cleanup campaign or resetting state.

## Clean start (new campaign)

```yaml
schema_version: 1
status: PLANNING
discovery_complete: false
current_iteration: ""
awaiting_pr_iteration: ""
intent_summary: "Incremental runtime-dead code removal; one iteration per spawn; one PR per iteration on garbage-collection-<slug>."
last_actor: ""
updated_at: "YYYY-MM-DDTHH:MM:SSZ"
campaign_stats:
  total: 0
  complete: 0
  blocked: 0
  deferred: 0
  pending: 0
iterations: []
```

## After discovery (ready for iteration 2+)

```yaml
schema_version: 1
status: AWAITING_PR
discovery_complete: true
current_iteration: "2"
awaiting_pr_iteration: "1"
intent_summary: "Describe the approved dead-code cleanup campaign."
last_actor: garbage-collector
updated_at: "YYYY-MM-DDTHH:MM:SSZ"
campaign_stats:
  total: 3
  complete: 1
  blocked: 0
  deferred: 0
  pending: 2
iterations:
  - id: "1"
    slug: component-name
    title: "Unused component"
    status: complete
    risk: low
    scope:
      files:
        - "path/to/unused_module.py"
      symbols:
        - "unused_symbol"
      docs:
        - "docs/path/to/canonical-document.md"
    evidence:
      entrypoint_checks:
        - "wired/entrypoint.py — no import or registration of unused_symbol"
      reference_grep:
        - "rg unused_symbol — zero hits outside iteration scope"
      why_dead: "No wired production path imports or invokes the component."
    verification:
      commands:
        - "pytest tests/path/to/relevant_tests.py -q"
    cleanup_notes: "Summarize the focused removal and any canonical documentation update."
    completed_at: "YYYY-MM-DDTHH:MM:SSZ"
    git:
      branch: ""
      base_branch: main
      commit_sha: ""
      pr_number: null
      pr_url: ""
      pr_status: pending
      pr_created_at: ""
  - id: "2"
    slug: legacy-foo-tool
    title: "Unused agent tool module"
    status: pending
    risk: medium
    scope:
      files:
        - "agent/tools/legacy_foo.py"
      symbols:
        - "legacy_foo_tool"
      docs: []
    evidence:
      entrypoint_checks:
        - "agent/tools registry/resolver — tool id not registered"
      reference_grep:
        - "rg legacy_foo — only self-references in agent/tools/legacy_foo.py"
      why_dead: "Tool never registered in wired resolver path."
    verification:
      commands:
        - "pytest tests -k tool_registry -q"
    cleanup_notes: ""
    completed_at: ""
    git:
      branch: ""
      base_branch: main
      commit_sha: ""
      pr_number: null
      pr_url: ""
      pr_status: pending
      pr_created_at: ""
  - id: "3"
    slug: old-loader-defer
    title: "Deferred ambiguous dynamic import"
    status: deferred
    risk: high
    scope:
      files:
        - "backend/services/old_loader.py"
      symbols: []
      docs: []
    evidence:
      entrypoint_checks:
        - "importlib string reference in backend/config/feature_flags.py — needs manual trace"
      reference_grep: []
      why_dead: "Likely dead but dynamic import path not fully traced."
    verification:
      commands: []
    cleanup_notes: "Defer until feature flag loader is traced in a dedicated iteration."
    completed_at: ""
    git:
      branch: ""
      base_branch: main
      commit_sha: ""
      pr_number: null
      pr_url: ""
      pr_status: pending
      pr_created_at: ""
```

## After PR created (ready for next iteration)

```yaml
status: READY
awaiting_pr_iteration: ""
last_actor: garbage-collection-workflow
updated_at: "YYYY-MM-DDTHH:MM:SSZ"
# iteration 1 git block:
#   branch: garbage-collection-<slug>
#   commit_sha: <commit-sha>
#   pr_number: <number>
#   pr_url: https://github.com/<owner>/<repository>/pull/<number>
#   pr_status: open
#   pr_created_at: "YYYY-MM-DDTHH:MM:SSZ"
```

## Blocked iteration

```yaml
status: BLOCKED
current_iteration: "2"
# ... keep iterations list ...
# Set blocked iteration:
#   status: blocked
#   cleanup_notes: "Still imported by backend/services/foo/bar.py:42 via lazy import."
```

## All complete

```yaml
status: ALL_COMPLETE
discovery_complete: true
current_iteration: ""
campaign_stats:
  total: 3
  complete: 2
  blocked: 0
  deferred: 1
  pending: 0
```

## Status values

| Status | When |
|--------|------|
| `PLANNING` | Empty or pre-discovery state |
| `READY` | Discovery done; about to start an iteration |
| `IN_PROGRESS` | Active iteration cleanup |
| `AWAITING_PR` | Iteration cleanup done; main agent must commit + open PR |
| `READY` | PR recorded; safe to spawn next iteration |
| `ALL_COMPLETE` | No pending iterations (blocked/deferred may remain) |
| `BLOCKED` | Active iteration failed verification or proof gap |
| `NEEDS_CLARIFICATION` | Missing user input |

## Rules

1. One cleanup iteration per `@garbage-collector` spawn (except first run: discovery + iteration 1).
2. Re-validate wired entrypoints on every iteration — do not trust stale discovery alone.
3. Never delete when runtime use is unproven; use `blocked` or `deferred`.
4. Record verification commands and results in `cleanup_notes` or iteration fields.
5. Each completed iteration gets branch `garbage-collection-<slug>` and its own PR; never commit GC work to `main`.
