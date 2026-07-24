# Test Coverage Matrix

## Current Baseline

The generated audit currently finds **1,189 test files**: 1,024 pytest files, 142 Vitest files, 10 `node:test` files, and 13 Playwright files. Thirty-one files contain selections used by required PR checks and another five are selected only by the manual `main` tier. All ten `node:test` fixture contracts and the `@pr-core` browser spec run in required PR E2E CI, while the four environment-independent fixture contracts also run in the required quick release gate. Eleven `@journey` specs run after merge on `main`/`master`, with the complete browser matrix reserved for release branches and manual certification; one `@runtime-local` spec remains in nightly/manual release certification, and one isolated artifact-policy probe is owned by its fixture integration contract. Exact file-level duration evidence exists for two browser specs; aggregate scoped-suite timings remain in audit notes rather than being assigned to individual files.

See [`generated/test-inventory-summary.md`](generated/test-inventory-summary.md) for generated counts and [`generated/test-inventory.csv`](generated/test-inventory.csv) for file-level records.

## Product Risk Matrix

| Product capability | Existing lower-layer evidence | Existing browser/system evidence | Current release confidence | Primary gap |
|---|---|---|---|---|
| Authentication and tenant context | Selected backend authorization and frontend session/tenant contracts | PR core authenticates and denies cross-user task access; scoped setup/owner matrix passed 6/6 across three browsers in about 1.4m | Required PR core passed the local and post-fix CI stability thresholds | Continue monitoring the required check; broader setup coverage remains post-merge/release evidence |
| Task lifecycle and authorization | Selected task-router authorization plus deterministic lifecycle, typed interrupt, and scoped file-browser contracts | `@journey` drives pause, resume, stop/cancellation, restart, failure recovery, completion, refresh persistence, isolated deletion, approval, rejection, clarification, duplicate-resume rejection, task-local interrupt isolation, and all three dashboard workspaces | Two complete 45-case matrices passed, including GitHub Actions on 2026-07-16 | Real Docker lifecycle remains separately owned by the runtime canary |
| Chat and runtime streaming | Selected stream schema, frontend stream client/store, and LangGraph quick markers | PR core and owner journey cover ordered deterministic streaming, tool output, observations, refresh/navigation persistence, and deep reasoning | Consecutive complete-suite certification met | Credentialed live-LLM behavior remains environment-specific certification work |
| Agent graphs and prompts | Large agent/core pytest surface; only quick LangGraph marker selections are required | Deterministic chat/deep-reasoning browser scenarios are manual | Partial | Most agent tests are untriaged and main/nightly marker suites are not wired to CI |
| Runtime-provider dispatch | Selected provider contracts and registry; local-runtime prerequisite/cleanup contracts pass | Real local-Docker Chromium canary is implemented and fails closed off Linux | Contract and fail-closed behavior proven | One clean supported Linux Docker pass remains; managed runner is a separate program |
| Managed runner control | Five runner/runtime files are manual-main; broader runner suite is untriaged | No required registration-to-execution journey | Low for release | Nightly runner registration, assignment, execution, interruption, and recovery |
| Local Docker and Kali execution | Runtime-image, Docker, executor, tool, prerequisite, ownership, and cleanup contracts exist | `@runtime-local` covers UI task/container lifecycle, terminal command, `/workspace` preview, cross-task isolation, and leak checks | Canary implemented; no live certification | Current non-Linux host returns `platform/linux_required`; clean Linux Docker pass remains |
| Workspace, files, and artifacts | File-browser safe resolver, runtime-provider scope, and offline seed contracts are covered | `@journey` previews suite-owned task-local text, rejects traversal, and proves another task's filename/content is absent in Chromium, Firefox, and WebKit | Strong for deterministic local workspace browsing | Real runtime artifact promotion/download remains part of local-Docker certification |
| Knowledge and evidence | Projection, query, router, evidence-read, offline-seed, and tier contracts cover the persisted boundary | `@journey` covers all five Knowledge tabs, finding-to-evidence preview, linked asset/service and provenance, territory topology, persisted readback, viewer non-disclosure, and direct 403/404 negatives in Chromium, Firefox, and WebKit | Strong deterministic persisted coverage | Real runtime ingestion remains separate from the deterministic journey |
| Reporting | Worker, generator, selection, router, deletion, read, offline-input, and frontend contracts cover the persisted boundary | `@journey` creates two worker-generated versions, observes progress, previews and downloads content, opens history, deletes and undoes deletion, and verifies authenticated persistence in Chromium, Firefox, and WebKit | Strong deterministic lifecycle coverage | Credentialed live-LLM report quality remains environment-specific certification work |
| Setup, Usage, Profile, and Settings | Frontend/backend and offline seed contracts cover the wired boundaries | PR core derives hosted providers from the live catalog and verifies one shared settings card/status per provider; fresh-install setup and remaining-pages journeys cover every top-level page, safe preference persistence, secret masking, archive/restore, and cleanup | Required provider/settings parity plus complete multi-browser deterministic evidence | Real deployment and credentialed-provider setup remain separate certification concerns |
| Deployment and packaging | Production frontend build is required; packaging/static tests are untriaged | No clean-host certification | Build-only | Install, migration, startup, runtime package, and upgrade certification |
| Security and isolation | Selected tenant/task authorization plus viewer, tenant, workspace, and runtime contract coverage | PR core proves cross-user denial; `@journey` covers viewer restrictions, direct API negatives, cross-tenant non-disclosure, and cache clearing | Required PR evidence plus complete multi-browser deterministic certification | Real-runtime and managed-runner isolation remain separate certification concerns |

## Current Gate Map

### Required pull-request gate

`npm run test:release:quick` currently proves:

- selected backend auth, tenant, runtime-provider, WebSocket ownership, and stream schema contracts;
- tests carrying the `regression_quick` LangGraph marker;
- selected frontend auth, stream subscription, packet ingestion, chat-store, and message rendering contracts;
- environment-independent actor, artifact-redaction, runtime-prerequisite, and sanitized-log fixture contracts through Node's test runner;
- TypeScript compilation;
- production frontend/server build.

It does not execute browser journeys, real Docker, managed runner, Kali executor, or clean-install tests. The complete ten-file fixture suite, including live-stack and authenticated-artifact integration contracts, runs in the separate PR E2E workflow after Chromium installation.

Playwright PR core is a separate required `e2e-smoke` workflow job so browser failures remain isolated from the release-gate output. The full deterministic journey workflow does not run on pull requests: Chromium runs after pushes to `main`/`master`, while the three-browser matrix runs on `release/**` pushes and manual dispatch.

### Manual main gate

`npm run test:release:main` adds selected runner-control security/protocol and runtime-provider context files. It continues to run the LangGraph quick marker rather than the LangGraph main marker.

### Browser and runtime tier map

| Command | Selection and ownership | Environment / exclusions |
|---|---|---|
| `npm run test:e2e:fixture-contracts:quick` | Four environment-independent `node:test` fixture/security files; required release gate | No browser, live backend, external service, or Docker daemon |
| `npm run test:e2e:fixture-contracts` | All ten `e2e/fixtures/*.test.ts` contracts; PR E2E workflow | Chromium and loopback are available for the two integration contracts; no external LLM or credentials |
| `npm run test:e2e:pr` | Five `@pr-core` cases, Chromium, PR workflow | Isolated deterministic app stack; excludes multi-browser and real-runtime work |
| `npm run test:e2e:journeys:chromium` | Sixteen `@journey` cases; post-merge `main`/`master` workflow | Chromium; no external LLM, Docker, browser interception, or external credentials |
| `npm run test:e2e:journeys:all` | Sixteen `@journey` cases per browser, 48 total; release-branch and manual certification workflow | Chromium, Firefox, WebKit; no external LLM, Docker, browser interception, or external credentials |
| `npm run test:e2e:runtime:local` | One `@runtime-local` Chromium case; nightly/manual and explicit release-certification jobs | Supported Linux plus real Docker; missing prerequisites fail rather than skip |
| `npm run test:release:e2e` | Manual `main` release contracts plus the isolated Chromium PR core | Convenience aggregate; not multi-browser or Linux Docker certification |

All Playwright tiers use one worker and zero retries. Workflow artifacts are failure-only and secret-safe; network traces remain disabled.

## Certification Evidence

| Required evidence | Current result | Release effect |
|---|---|---|
| Three local PR-core passes | Met; six recorded runs span 11.4-12.3s | Local stability complete |
| Three post-fix PR-core CI passes | Met; more than three successful runs are recorded | `e2e-pr-core` is a required ruleset check |
| Two consecutive complete 45-case journey runs | Met: local 45/45 on 2026-07-12 and GitHub Actions 45/45 on 2026-07-16 | Multi-browser stability certification complete |
| One clean supported Linux Docker run with no leaked resources | Pending; the first scheduled run exposed a final-read polling bug that PR #2 fixed | Rerun nightly/manual certification before treating the canary as release evidence |

The complete browser matrix now runs on the current host. The runtime command
still fails closed on this non-Linux host. The first scheduled Linux run built
the image and reached the UI lifecycle before exposing the now-fixed polling
boundary defect, so successful real local-Docker certification remains pending.

## Audit Queue

The inventory should be triaged in this order:

1. Validate and time the 23 required-PR selections and 5 manual-main selections.
2. Complete and time the 13 Playwright files, retaining aggregate timings only where per-file timing was not captured.
3. Audit security, tenant/task isolation, runtime-provider, runner, workspace, artifact, and protocol tests.
4. Audit task/chat, agent graph, prompt, persistence, reporting, and knowledge suites.
5. Audit frontend component and utility suites.
6. Confirm duplicate or legacy candidates through wired-call-path inspection before deletion.

## Exit Criteria For Step 1

- Every version-controlled or non-ignored working-tree test file appears in the generated CSV.
- Every file has framework, owner, product area, layer, gate membership, evidence status, duration field, and notes field.
- Unknown evidence is represented explicitly rather than guessed.
- Current commands and their limitations are documented.
- Reviewed status/timing data has a persistent non-generated home.
- Generated outputs can be checked for drift.

This completes inventory foundation, not test certification. The large `untriaged` count is the measured backlog for subsequent audit work.
