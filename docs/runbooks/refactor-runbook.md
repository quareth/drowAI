# Refactor Rules (Binding)

**Document Version**: 1.1  
**Created**: 2026-05-29  
**Status**: Authoritative  
**Applies to**: every refactor guide and every implementation under `docs/refactor/`

> **Purpose.** Define the non-negotiable rules that govern how refactors are
> planned and executed in this repository.
>
> **Scope.** Two audiences, one rulebook: (1) **refactor guides/plans** must be
> written to comply with these rules and must embed them as phases and
> acceptance criteria; (2) **implementations** of those guides must follow them
> at every task and gate.
>
> **Boundaries.** These rules govern *process and safety*, not feature design.
> They do not prescribe the target architecture of any specific refactor — that
> lives in each program's statement, README, and phase guides. Where a guide and
> these rules conflict, **these rules win**; the guide must be corrected.
>
> **Program-specific supplements.** Rename or cutover programs may add local
> `safety-rules.md` files with extra gates (inventory, naming maps, grep
> patterns). Local rules extend — never weaken — these binding rules.

---

## How to use this document

- **Authoring a guide:** every refactor guide must satisfy all rules below and
  must explicitly include (a) a test-baseline step, (b) the appropriate migration
  sequencing for the refactor class (see Rules 3 and 8), and (c) a final **Review
  & Cleanup** phase with the acceptance criteria from
  [Rule 7](#rule-7--mandatory-review--cleanup-phase).
- **Implementing a guide:** every task and gate must honor these rules. A refactor
  is "done" only when the Review & Cleanup acceptance criteria pass.
- A guide or implementation that cannot satisfy a rule must **stop and ask**, not
  silently deviate.

---

## Core principle

**A refactor changes structure or identifiers, never behavior.** Observable
system behavior must match the pre-refactor baseline unless the program
explicitly scopes identifier/string changes (e.g. a rename cutover) and documents
them in a naming map. If a change would alter product behavior, it is not a
refactor — split it out and raise it explicitly.

---

## The Rules

### Rule 0 — Zero behavior change

- Refactors **must not** change product behavior.
- **Structural refactors** (extract, split, relocate): function and class bodies
  are moved **verbatim**; no edits to signatures, names, defaults, control flow,
  ordering of effects, or error/return semantics. Permitted edits: relocation,
  `import` statements, and repointing references to the new canonical location.
- **Identifier rename programs**: symbols, env keys, wire values, or persisted
  strings may change only as documented in the program naming map; all producers
  and consumers must update atomically within the phase boundary.
- If a behavior change is discovered to be necessary, it is **out of scope** for
  the refactor and must be tracked separately.

### Rule 1 — Safety first

- Prefer the smallest, most reversible step that proves the change is safe.
- Never delete anything until its replacement is proven in place (see Rules 3
  and 4).
- Keep changes surgical: touch only what the task requires; do not opportunistically
  refactor adjacent code.

### Rule 2 — Test the baseline, lock it, re-verify at the end

- **Before** implementing a task, identify the relevant tests. If adequate tests
  do not exist, **create them first** to capture current behavior.
- **Run** those tests against the pre-change code and **lock the results** as the
  baseline (the expected outcome the refactor must reproduce).
- **After** the task/phase, run the **same** tests again. The results must match
  the locked baseline exactly. Any difference is a Rule 0 violation and must be
  fixed before proceeding.
- Tests created to lock a baseline are kept (not throwaway).
- Assertions that encode old identifier names may change only where they
  literally assert renamed strings — functional outcomes must remain equivalent.

### Rule 3 — Structural sequencing: extract, migrate, remove

For **structural** refactors (module extraction, monolith decomposition):

1. **Extract** — create the new structure/modules **alongside** the legacy code
   (temporary duplication is expected and acceptable in this window).
2. **Migrate** — repoint **every** caller/reference to the new location and
   verify the system runs entirely on the new structure.
3. **Remove legacy** — only after migration is complete and green, delete the
   old code.

Legacy is **never** removed before the new path is proven and all references
are migrated.

### Rule 4 — No fallbacks, no backward compatibility, no new flags

- **No fallbacks.** Do not add "try new, fall back to old" paths. The transition
  is direct.
- **No backward-compatibility shims.** Do not keep old import paths, re-exports,
  or aliases alive for old callers — repoint the callers instead.
- **No new flags.** Do not introduce feature flags, env switches, or config
  toggles to gate the refactor. (Internal imports a module needs for its own use
  are normal usage, not a compatibility layer.)

### Rule 5 — Quality: no monoliths, clear boundaries

- **No monolithic files, classes, or functions** in the resulting structure.
  Each new file/module is single-responsibility and small.
- **Mandatory module docstring.** Every new file opens with a docstring (first
  lines) stating its **purpose** and **scope boundary**. Code in the file must
  not violate that stated boundary.
- **Single source of truth.** After migration, each moved symbol is defined
  **exactly once**, in its new home — no residual or duplicate definition.
- **Clean imports.** Absolute imports; stdlib → third-party → local ordering;
  no import cycles (dependencies point one direction).
- **Separation of concerns.** Keep orchestration, domain models, I/O, and helpers
  in distinct modules; do not mix layers in one file.

### Rule 6 — Documentation discipline

- Each refactor program has a **statement** or **README** (problem + scope) and
  one or more **phase guides**. All open with a Purpose / Scope / Boundaries
  block.
- Guides must include a **symbol/change inventory** (what moves or renames where),
  a **dependency/no-cycle** check where applicable, **verification gates**, and
  the **Review & Cleanup** phase.
- When structure or public contracts change, update the nearest `docs/architecture/*`
  page briefly and code-verified.

### Rule 7 — Mandatory Review & Cleanup phase

- **Every implementation guide must end with a dedicated Review & Cleanup phase**
  that runs after the refactor scope of that guide is complete.
- This phase verifies the refactor ended cleanly and removes all legacy left over
  from the guide's scope. **No dead code may remain.**
- The cleanup phase is **acceptance criteria** for declaring the refactor done —
  a guide is not complete until it passes.

#### Review & Cleanup acceptance criteria (must all pass)

- [ ] No dead code remains in scope: no orphaned legacy definitions, unreachable
      branches, unused imports/vars, or commented-out code introduced or left by
      the refactor.
- [ ] No duplicate definitions: every moved symbol is defined exactly once
      (single source of truth, Rule 5).
- [ ] No fallbacks, no compatibility shims/re-exports, no new flags exist
      (Rule 4).
- [ ] Every caller/reference points at the new canonical location; nothing still
      imports from the removed legacy path.
- [ ] Every new file has a purpose+boundary docstring and respects it (Rule 5).
- [ ] The locked baseline tests (Rule 2) are re-run and match exactly — zero
      behavior change confirmed (Rule 0).
- [ ] The full relevant test suite is green.
- [ ] Program grep gates pass (when the guide defines them).

### Rule 8 — Identifier rename programs

When a program renames symbols, env keys, wire protocol strings, or DB objects
(in addition to or instead of structural extraction):

- **Discovery first** — inventory and naming map finalized before code edits.
- **Layered phases** — lowest-coupling layers before contract layers; order is
  documented in the program guides.
- **Atomic contract slices** — wire, env, and schema renames land with all
  producers and consumers in the same change set.
- **Frozen exemptions** — document what must not change (e.g. historical migration
  filenames, out-of-scope paths).
- **Forward migrations only** — never rewrite historical Alembic revision files;
  add new revisions for schema/object renames.
- Program-local `safety-rules.md` may add grep gates and merge-order rules.

---

## Process checklist (per task)

For each task in a refactor guide, the implementer must:

1. [ ] Identify/author relevant tests; run and **lock the baseline** (Rule 2).
2. [ ] Apply the program's migration sequence (Rule 3 for structural; Rule 8 for
      rename layers).
3. [ ] **Migrate** all references; verify the system runs on the new structure or
      names (Rules 3–4, Rule 8).
4. [ ] **Remove legacy** only after migration is green (Rule 3, Rule 4).
5. [ ] Re-run the locked tests; confirm functionally equivalent outcomes (Rule 0, Rule 2).
6. [ ] Confirm quality gates: no monolith, docstrings, single source of truth, no cycles (Rule 5).

A guide's final phase then runs the [Review & Cleanup acceptance criteria](#review--cleanup-acceptance-criteria-must-all-pass) over the whole guide scope.

---

## What NOT to do

- ❌ Change behavior "while you're in there".
- ❌ Delete legacy before the new path is proven and all callers are migrated.
- ❌ Add a fallback path, compatibility shim/re-export, alias, or feature flag.
- ❌ Leave dead code, duplicate definitions, or unused imports behind.
- ❌ Create monolithic files/classes/functions, or files without a
      purpose+boundary docstring.
- ❌ Skip the test baseline, the end re-verification, or the Review & Cleanup phase.
- ❌ Mix unrelated contract changes into a structural-only or rename-only phase.

---

## Related documentation

- `docs/refactor/<program>/` — per-program statements, phase guides, and optional
  `safety-rules.md` / `naming-map.md`
- `AGENTS.md` — repo conventions for assistants and implementers

## Document History

| Version | Date | Changes |
|---------|------|---------|
| 1.0 | 2026-05-29 | Initial binding refactor rules. |
| 1.1 | 2026-06-10 | Moved to `docs/refactor/RULES.md`; generalized for all refactor programs; added Rule 8 for identifier rename programs. |
