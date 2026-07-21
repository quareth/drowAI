# Test Strategy

## Purpose

This document defines how DrowAI turns a large historical test surface into understandable release evidence. The goal is not to run every test in every pull request. The goal is to know which product risk each test covers, which environment it requires, and which failures block a pull request, `main`, a nightly run, or a public release.

## Current Test-Suite Maturity

> **Important:** DrowAI contains a large historical test surface that is still
> being audited. The generated inventory currently records 1,173 test files;
> 1,125 are `untriaged`, 31 contain trusted CI selections, ten are useful slow
> journeys, one is candidate E2E coverage, five are curated manual coverage,
> and one is environment-dependent.
> Only the documented curated gates currently represent release evidence. The
> repository does not claim that every historical test passes as one aggregate
> suite.

An `untriaged` test is not automatically broken or obsolete. It may provide
useful coverage, require an environment that is not available in the required
gate, duplicate newer coverage, exercise a disconnected legacy path, or need
repair. Investigate and record evidence before classifying, skipping, or
removing it.

The generated inventory is the source of truth for the version-controlled and non-ignored working-tree test-file list:

- [`generated/test-inventory-summary.md`](generated/test-inventory-summary.md) contains counts and current evidence status.
- [`generated/test-inventory.csv`](generated/test-inventory.csv) contains one record per tracked test file.
- [`test-audit-overrides.json`](test-audit-overrides.json) records reviewed classifications and measured timing evidence without editing generated files.

Regenerate the inventory with:

```bash
.venv/bin/python scripts/generate_test_inventory.py
```

Verify that committed inventory files are current with:

```bash
.venv/bin/python scripts/generate_test_inventory.py --check
```

## Foundational Rule

Do not use browser E2E tests to prove every module independently.

- Unit tests prove local behavior and edge cases.
- Contract tests prove schemas, protocols, and stable boundaries.
- Integration tests prove collaboration between real components.
- Browser E2E tests prove a small number of complete user journeys.
- System tests prove Docker, runner, Kali, storage, and deployment behavior in realistic environments.

A module is release-covered when its important risks are covered at the lowest reliable layer and the product journey that depends on it is covered at an appropriate higher layer.

## Evidence Status

Inventory status describes evidence, not an opinion about code quality.

| Status | Meaning |
|---|---|
| `trusted-ci-selection` | At least part of the file is selected by the current required PR release gate. Marker-selected files may also contain tests that are not selected. |
| `curated-manual` | The file is selected by a maintained command, but that command is not currently required by GitHub CI. |
| `candidate-e2e` | The scenario is intended for release confidence but has not completed CI stabilization. |
| `useful-slow` | Audit evidence shows the test is useful but unsuitable for the fast PR gate. |
| `environment-dependent` | The test requires a browser, service, container, credential, host capability, or pre-started stack. |
| `flaky` | Repeated execution has produced nondeterministic results. Record reproduction evidence in `notes`. |
| `duplicate` | The same risk is already proven elsewhere with no distinct value. Do not delete until the duplicate relationship is reviewed. |
| `legacy-disconnected` | Code-path inspection shows the tested path is not wired into the current product. Do not delete until confirmed. |
| `untriaged` | No gate membership or completed audit currently establishes release ownership. |

Never mark a test `trusted-ci-selection`, `useful-slow`, `flaky`, `duplicate`, or `legacy-disconnected` without command output or code-path evidence.

## Wired Commands And Tier Ownership

| Tier | Command and selector | CI ownership | Included environment | Explicit exclusions / status |
|---|---|---|---|---|
| Required release contracts | `npm run test:release:quick` | `.github/workflows/release-gate.yml` on pull requests | Curated backend, LangGraph, frontend, four environment-independent `node:test` fixture/security contracts, TypeScript, and build checks | No browser journey, Docker execution, managed runner, or Kali execution. |
| PR browser core | `npm run test:e2e:pr` (`@pr-core`, Chromium) | Required `.github/workflows/e2e-smoke.yml` check on pull requests | Real frontend/backend/WebSocket/SQLite with deterministic graph execution; one worker and zero retries | No external LLM, Docker, viewer journey, full settings/reporting/knowledge lifecycle, or multi-browser certification. |
| Deterministic journeys | `npm run test:e2e:journeys:chromium` on `main`/`master`; `npm run test:e2e:journeys:all` on `release/**` and manual dispatch | `.github/workflows/e2e-journeys.yml` after merge or on explicit certification | Isolated frontend/backend/database/workspaces plus offline process-scoped seeding; release/manual certification covers Chromium, Firefox, and WebKit | No real Docker, managed runner, live LLM, external credentials, mobile/Safari emulation, accessibility, or visual-regression certification. |
| Local-runtime canary | `npm run test:e2e:runtime:local` (`@runtime-local`, Chromium) | `.github/workflows/e2e-runtime-local.yml` nightly/manual, with a separate manual `release_certification` job | Clean supported Linux host, real Docker image/container/terminal/workspace, deterministic safe shell command | No external LLM or managed runner. Missing Linux, Docker daemon, image, or runtime capability fails explicitly. The first scheduled run found a final-read polling defect that PR #2 fixed; a successful clean-host rerun is pending. |

Failure artifacts are owned by each workflow and uploaded only after failure. They include screenshots, video, HTML output, sanitized service logs, and scenario metadata. Playwright network traces remain disabled because they can retain authorization and cookie headers.

`npm run test:e2e:fixture-contracts` executes all ten `node:test` fixture contracts in the PR E2E workflow after Chromium is installed. The required quick release gate uses `test:e2e:fixture-contracts:quick`, which selects the four contracts that need neither a browser nor a live loopback stack.

`test:release:e2e` is a maintained manual aggregate. It runs the `main`
release contracts and the isolated `test:e2e:pr` Chromium core; it does not
replace the multi-browser journey or Linux Docker certification tiers.

## Stability Evidence And Promotion Policy

| Evidence gate | Recorded result | Promotion state |
|---|---|---|
| PR core: three consecutive local passes | Met; recorded Chromium examples are 11.4s, 11.5s, 11.6s, 11.8s, 12.0s, and 12.3s for four cases | Local threshold met. |
| PR core: three consecutive post-fix CI passes | Met; more than three successful GitHub Actions runs are recorded after the polling fix | The repository ruleset requires `e2e-pr-core`. |
| Complete deterministic suite: two consecutive 45-case all-browser passes | Met: 45/45 locally on 2026-07-12 and 45/45 in GitHub Actions on 2026-07-16 | Multi-browser stability threshold met; retain the full matrix for release/manual certification. |
| Real runtime: one clean supported Linux Docker pass with leak-free teardown | Pending; the first scheduled Linux run built the image and exposed the now-fixed final-read polling defect | Rerun the nightly/manual canary before using it as release-certification evidence. |

Scoped evidence retained in the inventory is useful implementation proof: Phase 2 passed 6/6 browser cases in approximately 1.4 minutes, Phase 3 passed 9/9 in approximately 3.1 minutes, Knowledge passed 3/3 with no duration captured, and Reporting passed 3/3 in 51.5 seconds. The complete matrix passed 45/45 locally on 2026-07-12 and again in GitHub Actions on 2026-07-16. The optimized Chromium post-merge tier passed 15/15 locally in 2.6 minutes on 2026-07-16.

## Test Placement

Use the nearest existing test root for the production responsibility:

- `backend/tests/` for backend control-plane behavior;
- `agent/**/tests/` for agent graph, prompt, tool, and runtime behavior;
- `client/src/**/__tests__/` for frontend unit/component behavior;
- `core/**/tests/` for shared prompt, LLM, and runbook contracts;
- `tests/runner/` and `tests/runtime_shared/` for runner/runtime protocol behavior;
- `kali_executor/tests/` for in-runtime executor behavior;
- `e2e/tests/` only for complete browser journeys.

New tests should identify the risk they prove in their module docstring or test name. New browser specs must state the user journey in one sentence.

## Audit Workflow

For each `untriaged` file:

1. Confirm the tested production path is wired from a current entrypoint.
2. Run the smallest command that executes the file in its required environment.
3. Record duration and pass/fail evidence.
4. Identify the product area, layer, and intended tier.
5. Record reviewed changes in `test-audit-overrides.json`.
6. Regenerate the inventory.
7. Promote a test into a gate only after it is deterministic and its failure has an obvious owner.

Audit order is risk-based: security/isolation, runtime-provider and runner boundaries, task/chat streaming, workspace/artifacts, reporting/knowledge, then lower-risk presentation and utilities.

## Coverage Policy

Line coverage will be used as a discovery signal, not a release-quality score. Coverage thresholds should be introduced only after a clean baseline exists. Critical authorization, isolation, protocol, persistence, and runtime-dispatch code should have explicit branch/negative-path tests even when aggregate line coverage is high.

## Change Policy

- Behavior changes require a test at the lowest reliable layer.
- Protocol/schema changes require producer and consumer contract coverage.
- User-critical changes require updating an existing E2E journey or adding one focused journey.
- A flaky required test is a product problem: fix it or remove it from the required tier with recorded evidence; do not add retries to hide it.
- Generated inventory files must remain current whenever tracked tests or gate selections change.
