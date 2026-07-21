# implementation-quality-review-state.example.md

Template for `.codex/agents/implementation-quality-review-state.md`. The live state file is ignored by Git. Copy the clean state, set one branch or commit target, and let the first reviewer freeze the exact Git diff.

## Clean state

```yaml
schema_version: 2
status: READY_FOR_REVIEW
round: 0
implementation_state: ".codex/agents/implementation-state.md" # optional intent context only
guide: "" # optional intent context only; never expands scope
scope_summary: "Strict quality review of one frozen branch or commit diff."
intent_summary: "Behavior-neutral quality assessment and cleanup only."
last_actor: ""
updated_at: "YYYY-MM-DDTHH:MM:SSZ"

scope:
  kind: "branch" # branch | commit
  target_ref: "feature/my-branch" # branch name or commit id
  base_ref: "origin/main" # required for branch; ignored for commit
  locked: false
  resolved:
    target_sha: ""
    base_sha: ""
    merge_base_sha: "" # branch only
    diff_range: ""
    worktree_head_sha: ""
    resolved_at: ""
    changed_files: []

review_coverage:
  reviewed_files: []
  skipped_files: []

quality_policy:
  quality_only: true
  behavior_change_prohibited: true
  implementation_outcome_change_prohibited: true
  structural_cleanup_only: true
  out_of_scope_edits_prohibited: true
  new_implementation_files_prohibited: true
  security_out_of_scope: true
  functional_correctness_out_of_scope: true
  pre_existing_debt_out_of_scope: true
  small_to_medium_fixes_only: true
  large_refactors_are_non_blocking_suggestions: true
  refactor_suggestion_root: "docs/devdocs/refactor"

fresh_review_policy:
  required_after_fix: true
  active_findings_cleared_before_review: true
  reviewer_must_review_full_frozen_scope_each_round: true
  spawn_new_reviewer_agent_each_cycle: true
  no_prior_finding_context_for_reviewer: true

stop_conditions:
  no_fixable_findings: false

active_findings: []
refactor_suggestions: []
```

## Frozen branch scope

For `kind: branch`, resolve and persist:

```yaml
scope:
  kind: "branch"
  target_ref: "feature/my-branch"
  base_ref: "origin/main"
  locked: true
  resolved:
    target_sha: "0123456789abcdef0123456789abcdef01234567"
    base_sha: "1111111111111111111111111111111111111111"
    merge_base_sha: "2222222222222222222222222222222222222222"
    diff_range: "2222222222222222222222222222222222222222..0123456789abcdef0123456789abcdef01234567"
    worktree_head_sha: "0123456789abcdef0123456789abcdef01234567"
    resolved_at: "YYYY-MM-DDTHH:MM:SSZ"
    changed_files:
      - status: "M"
        path: "backend/services/example_service.py"
        old_path: ""
      - status: "R100"
        path: "client/src/new_name.ts"
        old_path: "client/src/old_name.ts"
```

This scope means all implementation changes between the branch merge base and the frozen branch-head SHA. Later commits on the branch are outside the current run.

## Frozen commit scope

For `kind: commit`, `target_ref` may be a full or abbreviated commit id. Resolve the exact commit and its first parent:

```yaml
scope:
  kind: "commit"
  target_ref: "0123456"
  base_ref: ""
  locked: true
  resolved:
    target_sha: "0123456789abcdef0123456789abcdef01234567"
    base_sha: "3333333333333333333333333333333333333333"
    merge_base_sha: ""
    diff_range: "3333333333333333333333333333333333333333..0123456789abcdef0123456789abcdef01234567"
    worktree_head_sha: "4444444444444444444444444444444444444444"
    resolved_at: "YYYY-MM-DDTHH:MM:SSZ"
    changed_files:
      - status: "A"
        path: "agent/tool_runtime/example.py"
        old_path: ""
```

This scope means only implementation introduced by that commit. For merge commits, use the first parent. For a root commit, use Git's empty tree as `base_sha`.

## Blocked state

```yaml
status: REVIEW_BLOCKED
round: 1
active_findings:
  - id: "Q1-P1"
    round: 1
    priority: "P1"
    severity: "blocker"
    category: "duplication_single_authority"
    title: "The scoped implementation introduced a second runtime identity resolver."
    status: "open"
    location:
      file: "backend/services/example_service.py"
      symbol: "resolve_runtime_identity"
      lines: "40-72"
    scope_evidence:
      target_sha: "0123456789abcdef0123456789abcdef01234567"
      changed_file: "backend/services/example_service.py"
      diff_hunk: "The frozen diff adds resolve_runtime_identity at lines 40-72."
    problem: "The scoped helper duplicates the established resolver and can drift from its placement rules."
    evidence:
      - "The frozen diff introduces the duplicate helper."
      - "backend/services/runtime_provider/registry.py:20 is the read-only contextual authority."
    maintenance_risk: "Two authorities must be updated together."
    violated_rule: "AGENTS.md — DRY and runtime-provider boundary."
    estimated_fix_scope: "small"
    required_fix: "Remove the scoped duplicate and delegate to the established resolver without editing that resolver."
    behavior_preservation: "Preserve provider selection, serialized identity, public contracts, and runtime results."
    verification:
      - "Run the focused runtime-provider service tests."
```

## Status routing

- `READY_FOR_REVIEW`: spawn a fresh quality reviewer.
- `REVIEW_BLOCKED`: call the quality fixer.
- `COMPLETE`: stop and continue the parent workflow.

The loop has no clarification or manual-stop status. Every candidate must resolve as behavior-neutral scoped cleanup, a non-blocking large-refactor suggestion, or no finding.

## Rules

1. Resolve and freeze the Git scope once; never recalculate it or replace its refs during the same run. Reset state before selecting another branch or commit.
2. `active_findings` contains only small/medium quality cleanup proven to originate in the frozen diff.
3. The fixer may edit only non-deleted `scope.resolved.changed_files` paths and may not create implementation files.
4. Surrounding code is read-only context and cannot become cleanup scope.
5. Pre-existing unrelated dirt, dead code, and duplication stay out of scope.
6. Behavior, public contracts, implementation intent, and runtime results must not change.
7. Clear active findings before every fresh review; keep no finding or fix history.
8. Keep only refactor suggestion paths as durable non-blocking outputs.
