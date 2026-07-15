# [Plan Title]

**Document Version**: 1.0  
**Created**: [YYYY-MM-DD]  
**Status**: [Draft | Planning | In Progress | Phase N Complete]  
**Author**: [Team or Author]

**How to use this template (for LLMs):** Replace all `[bracketed]` placeholders with concrete content. Keep the same structure and styling: H1 title, metadata, horizontal rules (`---`), tables for files/components/phases, code blocks with language tags, checkbox acceptance criteria (`- [ ]` / `- [x]`), and ASCII diagrams where useful. Be detailed enough to implement from the plan but avoid enormous length—use representative code and phase-level summaries. You can include short snippets to show structure.

---

## Table of Contents

1. [Executive Summary](#executive-summary)
2. [Current State Analysis](#current-state-analysis)
3. [Architecture Overview](#architecture-overview)
4. [Goals and Non-Goals](#goals-and-non-goals)
5. [Code Quality Standards](#code-quality-standards)
6. [Implementation Phases](#implementation-phases)
7. [Rollout and Migration](#rollout-and-migration)
8. [Success Criteria](#success-criteria)
9. [Appendix](#appendix)

---

## Executive Summary

### Goal

[One to three sentences describing the primary goal of this plan.]

### What This Plan Does

- [Bullet: scope item 1]
- [Bullet: scope item 2]
- [Bullet: scope item 3]

### What This Plan Does NOT Do

- [Bullet: explicit out-of-scope item 1]
- [Bullet: explicit out-of-scope item 2]

### Key Finding (if applicable)

[Optional: One paragraph summarizing the main insight or recommended approach.]

---

## Current State Analysis

### Problem Areas

1. **[Problem 1]**: [Short description]
2. **[Problem 2]**: [Short description]
3. **[Problem 3]**: [Short description]

### Files or Components Currently Affected

| File / Component | Issue or Role |
|------------------|---------------|
| `path/to/file.py` | [Description] |
| `path/to/other.py` | [Description] |

### Existing Components (Reuse Opportunities)

| Component | Location | Purpose | Reuse? |
|-----------|----------|---------|--------|
| [Name] | [Path] | [Purpose] | ✅ / ⚠️ / ❌ |

### Data or Control Flow (optional)

```
[ASCII diagram or step-by-step flow of current vs target behavior]
```

---

## Architecture Overview

### Target Architecture

```
┌─────────────────────────────────────────────────────────────────────────┐
│  [Component / Layer Name]                                                 │
│  [Responsibilities]                                                      │
└─────────────────────────────┬───────────────────────────────────────────┘
                              │ [relationship]
                              ▼
┌─────────────────────────────────────────────────────────────────────────┐
│  [Next Component]                                                        │
└─────────────────────────────────────────────────────────────────────────┘
```

### Directory or Module Structure (if applicable)

```
path/to/
├── module_a/
│   ├── __init__.py
│   └── component.py
└── module_b/
    └── ...
```

---

## Goals and Non-Goals

### Goals

1. [Goal 1]
2. [Goal 2]
3. [Goal 3]

### Non-Goals (Out of Scope)

1. ❌ [Explicit non-goal 1]
2. ❌ [Explicit non-goal 2]

---

## Code Quality Standards

### Requirements for New/Modified Code

1. **Type hints**: [e.g. Complete type annotations on public functions]
2. **Docstrings**: [e.g. Google-style with Args, Returns, Raises]
3. **Logging**: [e.g. Use `logger = logging.getLogger(__name__)`]
4. **Error handling**: [e.g. No silent failures; raise explicit exceptions]
5. **Imports**: [e.g. Standard library → third-party → local, absolute imports]

### What NOT To Do

- ❌ [Anti-pattern or forbidden approach 1]
- ❌ [Anti-pattern or forbidden approach 2]

### Error Handling (optional)

```python
# GOOD
try:
    result = do_thing()
except SpecificError as e:
    raise DomainError(f"Context: {e}") from e

# BAD
try:
    result = do_thing()
except:
    return None
```

---

## Implementation Phases

### Phase Overview

| Phase | Name | Scope | Estimated Effort |
|-------|------|-------|------------------|
| 1 | [Phase 1 name] | [Scope] | [Small / Medium / Large] |
| 2 | [Phase 2 name] | [Scope] | [Small / Medium / Large] |

### Dependency Order

```
Phase 1
    │
    ▼
Phase 2
    │
    ▼
Phase 3 ...
```

---

### Phase 1: [Phase Name]

**Goal**: [One sentence objective for this phase.]

#### Task 1.1: [Task Title]

**File**: `path/to/file.py`

[Brief description. Include code snippets only where they define the contract or key logic.]

```python
# Representative code or interface
def example():
    """Docstring."""
    pass
```

**Acceptance Criteria**:
- [ ] [Criterion 1]
- [ ] [Criterion 2]

---

#### Task 1.2: [Task Title]

**File**: `path/to/other.py`

[Description.]

**Acceptance Criteria**:
- [ ] [Criterion 1]
- [ ] [Criterion 2]

---

#### Phase 1 Tests

**File(s)**: `path/to/tests/test_phase1_feature.py`

Tests for this phase:

- [ ] [Test case or test class 1]
- [ ] [Test case or test class 2]

```python
# Optional: example test structure for this phase
class TestPhase1Feature:
    def test_[scenario](self):
        """[Description]."""
        ...
```

---

#### Phase 1 Acceptance Criteria

- [ ] [Phase-level criterion 1]
- [ ] [Phase-level criterion 2]
- [ ] All Phase 1 tests pass

---

### Phase 2: [Phase Name]

**Goal**: [Objective.]

#### Task 2.1: [Task Title]

**File**: `path/to/file.py`

[Description and any code snippets.]

**Acceptance Criteria**:
- [ ] [Criterion 1]
- [ ] [Criterion 2]

---

#### Phase 2 Tests

**File(s)**: `path/to/tests/test_phase2_feature.py`

Tests for this phase:

- [ ] [Test case or test class 1]
- [ ] [Test case or test class 2]

---

#### Phase 2 Acceptance Criteria

- [ ] [Phase-level criterion 1]
- [ ] All Phase 2 tests pass

---

### Phase N: [Further Phases]

[Repeat the same structure: Goal → Tasks (with File, description, Acceptance Criteria) → **Phase N Tests** (file(s), test cases for this phase) → Phase Acceptance Criteria.]

---

## Rollout and Migration

### Rollout Strategy

| Phase | Risk | Approach |
|-------|------|----------|
| [Phase] | [Low/Medium/High] | [Direct deploy / Feature flag / Staged] |

### Backward Compatibility

- [How existing behavior is preserved]
- [Default values or feature flags]

### Rollback Plan

1. [Step 1 to revert]
2. [Step 2 to revert]

### Metrics to Monitor (optional)

| Metric | Description |
|--------|-------------|
| [metric_name] | [What it measures] |

---

## Success Criteria

The plan is complete when:

1. [ ] [Criterion 1]
2. [ ] [Criterion 2]
3. [ ] [Criterion 3]
4. [ ] All tests pass
5. [ ] [Documentation / code review as needed]

---

## Future Work (Optional)

- [Item not in this iteration]
- [Follow-up improvement]

---

## Appendix

### File Change Summary

| Phase | File | Action |
|-------|------|--------|
| 1 | `path/to/file.py` | CREATE / MODIFY |
| 2 | `path/to/other.py` | MODIFY |

### Related Documentation

- [Link or path to related doc 1]
- [Link or path to related doc 2]

### Glossary (optional)

| Term | Definition |
|------|------------|
| [Term] | [Definition] |

### Document History

| Version | Date | Changes |
|---------|------|---------|
| 1.0 | [YYYY-MM-DD] | Initial version |

---

**End of Plan**
