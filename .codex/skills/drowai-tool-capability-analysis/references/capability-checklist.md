# Tool capability evidence checklist

Use this checklist for one exact tool ID. Record file paths and wired callers,
not broad architectural claims.

| Dimension | Required evidence | Blocking examples |
|---|---|---|
| Registry | `available_tools`, class-declared ID, concrete `BaseTool` | helper module mistaken for a tool; ambiguous IDs |
| Repository references | current registered/visible inventories plus responsibility-specific mature references with wired callers | fixed universal reference; file exists but is inactive; no selection rationale |
| Kali installation | provider-mediated `command -v`, installed path/version, disposable-task cleanup | host-only package check; missing executable; unknown cleanup |
| Official CLI contract | installed-version help/manual plus primary official versioned URLs | relying on latest docs for an older Kali package; third-party examples |
| Definition drift | args model, function schema/description, registry metadata, `build_command`, direct correction commit or deferred gap | stale/removed flag; missing subcommand; correction expands into a refactor |
| Execution schema | `args_model` JSON schema, validators, required/default fields | missing target; contradictory defaults; unsafe unconstrained input |
| Planner schema | provider-neutral function spec and resolver path | required fields removed or renamed |
| Runtime | `build_command`, workspace preparation, runtime-provider transport | host-side execution bypass; missing executable |
| Result contract | parse status, metadata, artifacts, success/empty/partial/failure | false-positive success; unstructured-only output |
| Semantics | normalized observations and bounded evidence | raw text presented as facts |
| Canonical facts | emitted observation/subject pairs are admitted by shared policy with canonical identity and masking | tool-id branch in compiler; unsupported or malformed subject identity |
| Compression | semantic envelope compiles through shared fact projection with exact selected/omitted counts and lossiness | per-tool adapter/registry; raw-output parsing in projection; unreported omission |
| PTR | finalized compact result reaches post-tool reasoning | result only visible in logs |
| Knowledge | ingestion and the shared bridge produce useful scoped observations/read models with archive-scoped evidence | per-tool adapter; compact/raw-artifact fallback; cross-task data or no meaningful fact |
| Tests/docs | parser, schema, security, runtime, compression, knowledge evidence | only mocked happy path |
| Visibility | allowlist and selected-category/function-spec reachability | exposed before mechanics are ready |

## Reference selection policy

Choose references from current wired code for each responsibility instead of
copying one tool wholesale:

- Tool/schema and command/runtime should match the candidate's CLI and execution
  family.
- Result, artifact, and semantic references should match its output shape.
- Canonical fact policy, Knowledge bridging, and compact projection use the
  shared reusable authorities; producer references should match the facts the
  tool emits.
- Visibility must follow the active catalog and function-spec path.

### Mature Nmap example

For network-scanning tools, verify Nmap first:

- Tool, args, command, parser, artifacts, and registration:
  `agent/tools/information_gathering/network_discovery/nmap.py`
- Semantics:
  `agent/tools/information_gathering/network_discovery/nmap_semantics.py`
- Canonical fact admission:
  `runtime_shared/semantic/pentest_facts/policy.py` and
  `runtime_shared/semantic/pentest_facts/compiler.py`
- Compression and PTR projection:
  `agent/graph/compression/pentest_facts/projection.py`,
  `agent/graph/compression/pentest_facts/presentation.py`, and the production
  caller in `agent/graph/compression/compressor.py`
- Knowledge:
  `backend/services/knowledge/pentest_facts/bridge.py`, the direct caller in
  `backend/services/knowledge/ingestion_service.py`, and the applicable focused
  projectors under `backend/services/knowledge/projection/`
- Visibility: `agent/tools/catalog_visibility.py`

Nmap remains a producer reference only for responsibilities where its active
contract is genuinely analogous. The compiler, Knowledge bridge, and compact
fact projection are shared authorities rather than Nmap-specific patterns.

### Reusable canonical consumers

For an existing fact family, a new producer must emit supported canonical rows
and rely on `compile_facts()`, the Knowledge bridge, and compact fact
projection. Do not add a per-tool Knowledge adapter, compression adapter,
registry entry, compressor import, or pentest `compact_*` override. Compact
selection and omission accounting belong to
`agent/graph/compression/pentest_facts/projection.py`; Knowledge consumes the
complete independently compiled fact set and is not constrained by compact
budgets.

## Evidence order

Follow registry/visibility inventory → responsibility-specific mature
references → expected executable → real Kali installation/version → official
CLI contract → definition drift/correction → schema → runtime → result
semantics → canonical fact admission → compression/PTR → Knowledge →
tests/docs → visibility. A tool hidden from the catalog may still have useful
foundation code; a visible tool with missing mechanics is a blocker.
