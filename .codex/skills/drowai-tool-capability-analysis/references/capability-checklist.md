# Tool capability evidence checklist

Use this checklist for one exact tool ID. Record file paths and wired callers,
not broad architectural claims.

| Dimension | Required evidence | Blocking examples |
|---|---|---|
| Registry | `available_tools`, class-declared ID, concrete `BaseTool` | helper module mistaken for a tool; ambiguous IDs |
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

## Amass reference map

Amass is the completed reference for shape and evidence, not a universal policy:

- Tool/schema: `agent/tools/information_gathering/dns/amass.py`
- Runtime: `agent/tools/information_gathering/dns/amass_runtime.py`
- Parser: `agent/tools/information_gathering/dns/amass_analysis.py`
- Semantics: `agent/tools/information_gathering/dns/amass_semantics.py`
- Shared semantic facts: `runtime_shared/semantic/amass_facts.py`
- Deterministic compression:
  `agent/graph/compression/deterministic/dns_discovery.py`
- Shared budgeting authority:
  `agent/graph/compression/deterministic/budget.py::budget_rendered_items`
- Knowledge: `backend/services/knowledge/adapters/amass_adapter.py`
- Visibility: `agent/tools/catalog_visibility.py`
- Primary focused tests: `tests/tools/test_amass_v5.py`,
  `tests/runtime_shared/test_amass_facts.py`, and
  `agent/graph/compression/deterministic/tests/test_dns_discovery.py`

Do not copy `build_amass_timeout_budget` or Amass inactivity semantics into a
different tool unless its own CLI/runtime behavior proves the same contract is
correct.

## Evidence order

Follow registry → schema → runtime → result semantics → compression/PTR →
knowledge → tests/docs → visibility. A tool hidden from the catalog may still
have useful foundation code; a visible tool with missing mechanics is a
blocker.
