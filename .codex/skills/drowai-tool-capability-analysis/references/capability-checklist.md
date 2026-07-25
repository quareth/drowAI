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
| Compression | deterministic adapter plus shared output budget | approximate omission counts; lossy status |
| PTR | finalized compact result reaches post-tool reasoning | result only visible in logs |
| Knowledge | registered adapter produces useful scoped facts | cross-task data or no meaningful fact |
| Tests/docs | parser, schema, security, runtime, compression, knowledge evidence | only mocked happy path |
| Visibility | allowlist and selected-category/function-spec reachability | exposed before mechanics are ready |

## Reference selection policy

Choose references from current wired code for each responsibility instead of
copying one tool wholesale:

- Tool/schema and command/runtime should match the candidate's CLI and execution
  family.
- Result, artifact, and semantic references should match its output shape.
- Compression/PTR and knowledge references should match the facts the tool
  produces.
- Visibility must follow the active catalog and function-spec path.

### Mature Nmap example

For network-scanning tools, verify Nmap first:

- Tool, args, command, parser, artifacts, and registration:
  `agent/tools/information_gathering/network_discovery/nmap.py`
- Semantics:
  `agent/tools/information_gathering/network_discovery/nmap_semantics.py`
- Compression and PTR projection:
  `agent/graph/compression/deterministic/network_discovery.py`
- Knowledge:
  `backend/services/knowledge/adapters/nmap_adapter.py`
- Visibility: `agent/tools/catalog_visibility.py`

Nmap remains a reference only for responsibilities where its active contract
is genuinely analogous.

### Amass budget exception

Amass is not the default architecture or behavior reference. Its only mandatory
reuse is the shared budgeting authority:
`agent/graph/compression/deterministic/budget.py::budget_rendered_items`.
Never duplicate that accounting, and never copy Amass-specific timeout,
inactivity, command, parsing, or semantic behavior.

## Evidence order

Follow registry/visibility inventory → responsibility-specific mature
references → expected executable → real Kali installation/version → official
CLI contract → definition drift/correction → schema → runtime → result
semantics → compression/PTR → knowledge → tests/docs → visibility. A tool
hidden from the catalog may still have useful foundation code; a visible tool
with missing mechanics is a blocker.
