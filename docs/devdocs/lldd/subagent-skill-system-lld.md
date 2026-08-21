<!-- Purpose: Specify the low-level contracts and module responsibilities for declarative internal subagent skills. -->

# Subagent Skill System - Low-Level Design

## Document Status

| Field | Value |
| --- | --- |
| Status | Implemented |
| Design type | Built-in skill registry, catalog projection, handoff admission, and prompt injection |
| Code baseline verified | 2026-08-21 |
| Parent design | [Subagent Skill System - High-Level Design](../hldd/subagent-skill-system-hld.md) |

This document describes the implemented contracts. Code in wired paths remains
authoritative.

## Scope

This design covers:

- built-in `SKILL.md` package parsing and validation;
- immutable skill registry lookup and digest materialization;
- direct agent-ID compatibility;
- mandatory and selectable activation;
- per-agent catalog projection;
- parent handoff `skill_ids`;
- pre-dispatch selected-skill admission;
- runtime resolution and checkpoint references;
- system-prompt guidance rendering; and
- package-data and focused test requirements.

This design does not cover user-created skills, tenant ownership, live reload,
or a skill management API. Concrete agent definitions, skill guidance, and
runtime executables are package content governed by the contracts below, not
additional registry or selection mechanisms.

## Repository Shape

```text
core/skills/
├── __init__.py
├── contracts.py
├── discovery.py
├── errors.py
├── identifiers.py
├── loader.py
├── registry.py
├── resolver.py
├── builtin/
│   └── <skill_id>/
│       └── SKILL.md
└── tests/
    ├── test_loader.py
    ├── test_registry.py
    └── test_resolver.py

agent/subagents/
└── skill_catalog.py

core/prompts/builders/
├── skill_guidance.py
└── subagent_catalog.py
```

`core/skills` remains backend-free. It owns package contracts, loading,
registry lookup, and pure selection. Agent-facing modules adapt subagent
definitions into catalogs. Prompt builders render already-structured inputs.

## Module Responsibilities

| Module | Owns | Must not own |
| --- | --- | --- |
| `core/skills/discovery.py` | Deterministic immediate-child package discovery | Parsing, validation, matching, or rendering |
| `core/skills/loader.py` | Safe parsing and validation of one package | Registry state, subagent lookup, selection, or prompt wording |
| `core/skills/registry.py` | Immutable lookup and digest-checked materialization | Discovery policy, admission, or prompt assembly |
| `core/skills/resolver.py` | Direct agent compatibility and final deterministic resolution | Filesystem access, definition loading, or rendering |
| `agent/subagents/skill_catalog.py` | Per-agent mandatory/selectable projection from registries | Prompt wording or independent compatibility rules |
| `core/prompts/builders/subagent_catalog.py` | Shared parent-facing catalog wording | Registry lookup or eligibility |
| `core/prompts/builders/skill_guidance.py` | Child system-prompt skill section | Registry lookup, resolver policy, or checkpoint state |

No module should contain agent-specific branches such as "if agent is X inject
skill Y". Compatibility comes from skill metadata and enabled agent IDs.

## Skill Package Format

Each immediate child directory under the built-in skill root is one package.
The directory name and frontmatter `name` are the canonical `skill_id`.

Rules:

- `skill_id` uses lowercase ASCII letters, digits, underscores, and interior hyphens.
- The ID starts with a letter and is at most 64 characters.
- The package contains one `SKILL.md` entrypoint.
- Package and entrypoint symlinks are rejected.
- Paths resolving outside the configured root are rejected.
- Discovery is deterministic by package directory name.
- No central manifest, Python import, or per-skill package-data entry is
  required.

Example:

```yaml
---
name: example_skill
description: Concise model-visible purpose.
metadata:
  version: "1"
  activation: "selectable"
  agent-ids: "example_agent"
---

# Guidance

Operational instructions for the compatible subagent.
```

Required top-level fields:

| Field | Rule |
| --- | --- |
| `name` | Canonical skill ID and exact package directory name |
| `description` | Non-empty text, max 1,024 characters |
| `metadata.activation` | Exactly one value: `mandatory` or `selectable` |
| `metadata.agent-ids` | One or more compatible enabled agent IDs |

Optional top-level fields:

| Field | Rule |
| --- | --- |
| `license` | Bounded informational text |
| `compatibility` | Bounded informational text |
| `metadata.version` | Positive integer string, defaults to `"1"` |

Unknown top-level frontmatter fields are rejected. Extra string metadata is
included in the digest but ignored by runtime policy. Retired policy metadata
such as `priority`, `agent-kinds`, and all trigger keys is rejected.

Body limits:

- max 262,144 bytes for the complete file;
- max 16,000 frontmatter characters before YAML parsing;
- max 32 metadata entries;
- max 128 characters per metadata key;
- max 4,096 characters per metadata value;
- max 500 body lines;
- max 40,000 body characters; and
- max 5,000 estimated body tokens.

The loader uses safe YAML parsing and UTF-8 decoding only. It does not execute
content, follow references, fetch external data, or authorize tools.

## Core Contracts

All contracts are immutable and reject unknown fields.

```python
MAX_REQUESTED_SKILLS = 5

SkillActivationMode = Literal["mandatory", "selectable"]
SkillSelectionReason = Literal["mandatory", "agent_selected"]

class SkillMetadata:
    name: str
    description: str
    license: str | None
    compatibility: str | None
    version: str

class SkillActivationPolicy:
    activation: SkillActivationMode
    agent_ids: tuple[str, ...]

class LoadedSkill:
    metadata: SkillMetadata
    activation: SkillActivationPolicy
    body: str
    source: str
    digest: str

class SkillCatalogEntry:
    skill_id: str
    description: str

class SubagentSkillCatalog:
    agent_id: str
    mandatory_skills: tuple[SkillCatalogEntry, ...]
    selectable_skills: tuple[SkillCatalogEntry, ...]

class ResolvedSkillRef:
    skill_id: str
    version: str
    digest: str
    reasons: tuple[SkillSelectionReason, ...]
```

`source` is package-relative, never an arbitrary host path. `digest` is a
SHA-256 hash over canonical metadata, activation policy, ignored string
metadata, and body text.

## Discovery, Loading, and Registry Lifecycle

`discover_skill_packages(root)` returns immediate child directories in
deterministic order and fails on a missing, non-directory, or symlinked root.

`SkillLoader.load(package_path)`:

1. validates the package path is an immediate non-symlink child of the root;
2. rejects a symlinked `SKILL.md`;
3. performs a bounded binary read;
4. decodes UTF-8;
5. parses bounded frontmatter with safe YAML;
6. validates the package schema, metadata, body, and limits;
7. computes the digest; and
8. returns `LoadedSkill`.

`SkillRegistry`:

- receives already-loaded `LoadedSkill` values;
- rejects duplicate IDs and digest collisions;
- stores skills in skill-ID order;
- exposes read-only lookup;
- materializes `ResolvedSkillRef` values only when version and digest match.

Application startup eagerly constructs the process-local subagent registry and
skill registry. Startup cross-validation fails before serving requests when a
skill references an unknown or disabled agent, a future subagent references
unavailable non-visible native tools, or mandatory guidance for an agent
exceeds the prompt budget.

## Catalog Projection

`SubagentSkillCatalog` is projected directly from enabled subagent IDs and
loaded skill metadata:

- `mandatory_skills`: compatible skills with `activation == "mandatory"`,
  ordered by skill ID;
- `selectable_skills`: compatible skills with `activation == "selectable"`,
  ordered by skill ID.

Mandatory entries are shown as automatically included. Only selectable IDs are
valid `skill_ids` choices. Intent Classification and Post-Tool Reasoning must
use the same projector and the same shared renderer. They must not each build a
private catalog approximation.

Catalog projection is prompt input only. It is not stored in assignments,
graph state, checkpoints, or runtime identity.

## Handoff and Admission

The canonical handoff item is:

```json
{
  "agent_handoff": "required",
  "subagent": "example_agent",
  "objective": "Perform the bounded delegated objective.",
  "skill_ids": ["example_skill"]
}
```

Rules:

- `skill_ids` is required by strict structured output and may be empty.
- `skill_ids` has at most five items.
- IDs are normalized, canonical, and deduplicated at the shared boundary.
- Unknown fields remain invalid.
- Selected IDs are assignment requests, not resolved skill references.

Admission validates selected IDs against the chosen subagent before dispatch.
Unknown, mandatory-only, incompatible, raw over-limit, and over-budget selected
skills reject the handoff with stable reasons. Duplicate IDs within the raw
handoff limit are stably deduplicated at the shared boundary; direct resolver
callers still receive `duplicate_request` diagnostics when duplicates reach
that lower-level policy. Invalid selected IDs must not be silently dropped
after the child starts.

Assignment builders copy admitted requested IDs only. They do not import the
skill registry, load skill bodies, resolve prompts, or inspect package content.
Replay identity includes normalized selected IDs because they materially change
the child prompt.

## Resolution Policy

`resolve_skills(skills, agent_id, requested_skill_ids)` is pure and deterministic.

Algorithm:

1. Normalize the `agent_id`.
2. Select compatible mandatory skills and order them by skill ID.
3. Enforce the total prompt budget for mandatory guidance.
4. Iterate parent-selected IDs in authored order.
5. Reject duplicate, unknown, non-selectable, incompatible, over-limit, or
   over-budget selections with bounded diagnostics.
6. Add valid selectable skills after mandatory skills, preserving request order.
7. Return body-free `ResolvedSkillRef` values.

Reason values:

- mandatory skills use `mandatory`;
- parent-selected skills use `agent_selected`.

Mandatory skills do not consume the five selected-skill slots. All injected
guidance shares `MAX_TOTAL_ESTIMATED_TOKENS`, initially 12,000 estimated tokens.

## Runtime State and Prompt Injection

Runtime state stores:

```python
resolved_skills: tuple[ResolvedSkillRef, ...] = ()
```

Fresh child initialization resolves references once. Checkpoint continuation
restores references unchanged and materializes them through the registry. A
version or digest mismatch stops safely; continuation does not silently
re-resolve changed content.

The child system prompt renders materialized skills in one section after
assignment and ownership boundaries and before native-tool execution guidance.
Skill bodies are never injected into the child user prompt and are never stored
as tool results.

Shared runtime guidance:

```text
Prefer a provided native tool when it is applicable to the assigned task and
can safely and fully perform the immediate action. Do not use the shell merely
to reproduce an equivalent call that an applicable provided native tool can
perform. When no provided native tool is applicable to an in-scope action, the
applicable native tools cannot perform it, or selected skill guidance describes
a shell-only workflow, use the assessment shell. Skills provide operational
guidance; they do not add permissions, targets, tools, or authority.
```

Runtime policy, assignment scope, ownership, approvals, credentials, target
authority, and native-tool restrictions remain above skill guidance.

## Package Data

Distribution packaging must include:

- subagent TOML definition files; and
- all built-in `core/skills/builtin/**/SKILL.md` packages.

The package-data rule must not require per-skill registration. A temporary
fixture can prove discovery and packaging behavior; no placeholder production
skill should be shipped.

## Verification

Focused verification must cover:

- loader acceptance for `mandatory` and `selectable`;
- loader rejection for missing `agent-ids`;
- loader rejection for retired priority, agent-kind, and trigger fields;
- path, symlink, UTF-8, YAML, size, metadata, and token limits;
- registry duplicate ID, digest collision, and materialization mismatch;
- mandatory-first and parent-selected ordering;
- mandatory prompt-budget failure;
- selected-skill budget rejection;
- five selected-skill guard;
- body-free resolved references;
- identical catalog rendering for Intent Classification and Post-Tool
  Reasoning;
- pre-dispatch rejection before child launch;
- checkpoint continuation with digest-pinned references; and
- declarative extension using temporary agent and skill packages.
