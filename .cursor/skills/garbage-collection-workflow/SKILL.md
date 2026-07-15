---
name: garbage-collection-workflow
description: Runs DrowAI's state-driven garbage-collection workflow from `.cursor/agents/cleanup-state.md`. Use when the user asks to clean dead code, remove legacy/unused modules, run garbage collector, continue cleanup iterations, or spawn incremental repo garbage collection. After each iteration, commits to garbage-collection-<slug> and opens a PR.
---

# Garbage Collection Workflow

Use this skill to run incremental runtime-dead code removal through `@garbage-collector` and repo-local state files.

This skill preserves the state-driven behavior:
- `@garbage-collector` discovers phased iterations and completes exactly one cleanup iteration per spawn (first run: discovery + iteration 1).
- The main agent opens one PR per completed iteration on branch `garbage-collection-<slug>`.
- The loop continues until `ALL_COMPLETE`, `BLOCKED`, `NEEDS_CLARIFICATION`, or the user stops.

## Durable Files

- `.cursor/agents/cleanup-state.md` — campaign ledger, iterations, PR metadata, `awaiting_pr_iteration`.
- `.cursor/agents/cleanup-state.example.md` — template for new campaigns or resets.
- `.cursor/agents/garbage-collector.md` — subagent instructions for discovery/cleanup.

## Trigger Examples

- "garbage collect"
- "clean dead code"
- "continue cleanup"
- "next garbage collection iteration"
- "run all cleanup iterations"
- Command: `garbage-collection-workflow`

## Workflow

1. Read `.cursor/agents/cleanup-state.md`.
2. If missing or the user asked for a **fresh campaign**, initialize from `cleanup-state.example.md` (`status: PLANNING`, empty `iterations`, `discovery_complete: false`).
3. If `status: AWAITING_PR`, run **PR per iteration** (below) before spawning `@garbage-collector` again.
4. Spawn `@garbage-collector` with a short prompt:
   - New campaign: `Run garbage collection: discover iterations and complete iteration 1 if found.`
   - Continue: `Continue garbage collection from cleanup-state.md.`
5. After `@garbage-collector` returns, read updated `cleanup-state.md`.
6. Route by status:
   - `AWAITING_PR` → run **PR per iteration** immediately.
   - `READY` → spawn `@garbage-collector` for the next iteration when the user wants to continue.
   - `ALL_COMPLETE` → if `awaiting_pr_iteration` is set, run **PR per iteration** first; then summarize campaign and PR links; stop.
   - `BLOCKED` or `NEEDS_CLARIFICATION` → surface evidence from state; wait for user.
   - `IN_PROGRESS` → abnormal exit; respawn `@garbage-collector` or reset the iteration to `pending`.
7. Do not paste full state into the subagent prompt; state files are authoritative.

## PR per iteration (main agent)

Run when `status: AWAITING_PR` and `awaiting_pr_iteration` is set. `@garbage-collector` does **not** commit or open PRs.

1. Resolve iteration where `id == awaiting_pr_iteration`; branch = `garbage-collection-<slug>`.
2. Never commit GC work to `main`, `master`, or unrelated feature branches.
3. Prepare git (`git status`, `git diff`, `git branch --show-current`, `git log -3 --oneline` in parallel).
4. From `main`:

```bash
git fetch origin
git checkout main
git pull origin main
git checkout -b garbage-collection-<slug>
git add <iteration-scoped files> .cursor/agents/cleanup-state.md
git commit -m "$(cat <<'EOF'
chore(gc): remove runtime-dead code — <title> (iteration <id>)

EOF
)"
git push -u origin HEAD
gh pr create --title "chore(gc): <title> (iteration <id>)" --body "$(cat <<'EOF'
## Summary
- Remove runtime-dead code validated in garbage-collection iteration <id> (`<slug>`).
- Wired-path checks confirmed no production entrypoint references.

## Removed scope
- <bullets from iteration scope.files>

## Verification
- <commands and results from iteration verification / cleanup_notes>

## State
- Recorded in `.cursor/agents/cleanup-state.md`

EOF
)"
```

5. Update `cleanup-state.md` on the PR branch:
   - iteration `git`: `branch`, `commit_sha`, `pr_number`, `pr_url`, `pr_status: open`, `pr_created_at`
   - clear `awaiting_pr_iteration`
   - `status: READY` if `campaign_stats.pending > 0`, else `ALL_COMPLETE`
   - `last_actor: garbage-collection-workflow`
6. Before the next iteration: `git checkout main && git pull origin main`.

## Hard Rules

- Do not ask the user whether to open a PR when `status: AWAITING_PR` — proceed unless blocked.
- Do not paste full reports between agents; `cleanup-state.md` is authoritative.
- One cleanup iteration per `@garbage-collector` spawn (except first run includes discovery + iteration 1).
- One PR per completed iteration; branch prefix always `garbage-collection-`.
- Never bypass wired-entrypoint checks in AGENTS.md.
- Never commit `.env` or secrets.
- Do not delete code outside the active iteration scope in the main agent.

## User Controls

| User says | Main agent action |
|-----------|-------------------|
| "clean dead code" / "garbage collect" | Init or continue; one iteration unless user wants full loop |
| "continue cleanup" / "next iteration" | PR if `AWAITING_PR`, then spawn `@garbage-collector` once |
| "run all iterations" / "keep going" | Loop gc → PR → gc → PR until `ALL_COMPLETE` or `BLOCKED` |
| "reset cleanup" | Reinitialize from `cleanup-state.example.md` |

## Final Response

When the workflow stops, report:
- final `status` from `.cursor/agents/cleanup-state.md`,
- iteration id/slug/title and PR URLs for completed iterations,
- files removed and verification run for the latest iteration,
- pending/blocked/deferred counts,
- whether to spawn `@garbage-collector` again.
