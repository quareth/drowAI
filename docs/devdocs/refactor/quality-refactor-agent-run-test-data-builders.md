<!-- Purpose: Record consolidation of repeated backend and frontend agent-run test data. -->

# Agent-Run Contract Test Builders

**Status:** Complete

**Implementation commit:** `39e307b` (`test(agent-runs): centralize contract test builders`)

## Completed boundary

`backend/tests/agent_run_test_support.py` owns fresh builders for runtime
identity, assignment derived from identity, and result derived from assignment.
`client/src/features/agent-runs/test-data.ts` owns the equivalent frontend
projection builders.

Builders accept explicit overrides, copy nested collections, return independent
objects, and contain no fixture scope or mutable shared state. Scenario state,
HTTP clients, graph state, registry mutation, and assertions remain local to
their suites. Scenario-specific wrappers delegate to the shared builders.

## Preserved contracts

- Existing per-suite values and deliberate malformed-contract cases are
  preserved.
- Production contracts and validation are unchanged.
- Test collection, async markers, parameterization, and scenario ownership are
  unchanged.
- No generic factory framework or production dependency was introduced.

## Verification evidence

- Builder tests verify tenant/task derivation, linked result identity, and fresh
  nested values.
- Backend agent-run services, router, facade, handler, continuation, and
  subagent E2E suites passed: 159 tests.
- Frontend agent-run and generic MessageList activity suites passed: 56 tests.
- `npm run check` passed.
- The generated inventory verified 1,230 test files; support modules were not
  collected as tests.
