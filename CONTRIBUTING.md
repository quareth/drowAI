# Contributing to DrowAI

This document defines the requirements for contributing issues, documentation,
code, tests, and reviews to DrowAI. Maintainer operations are defined in
[MAINTAINING.md](MAINTAINING.md), version and compatibility decisions in
[RELEASING.md](RELEASING.md).

## Before You Start

- Read the project overview and local setup in [README.md](README.md).
- Check existing issues and pull requests before starting substantial work.
- For larger changes, open an issue first so the intended behavior and scope can
  be agreed before implementation.
- Report suspected vulnerabilities privately according to
  [SECURITY.md](SECURITY.md), not in a public issue.

Small bug fixes may go directly to a pull request. Open an issue before work
that changes a public API, storage or deployment behavior, security boundary,
or architecture. A maintainer agreeing that an idea is worth discussing does
not guarantee that its implementation will be merged.

## Branches and Commits

Create a short-lived branch from current `main`. Use a descriptive name such as
`fix/context-counter`, `feat/tool-catalog-filter`, or `docs/local-setup`.

Write commits that explain one coherent step. Work-in-progress commits are
permitted on topic branches because pull requests are squash-merged. The pull
request title becomes the permanent commit title and must use one of these
prefixes:

- `feat:` for user-visible functionality;
- `fix:` for a behavior correction;
- `security:` for a non-sensitive security improvement or disclosed fix;
- `docs:` for documentation only;
- `test:` for tests only;
- `refactor:` for behavior-preserving internal changes;
- `chore:` for maintenance;
- `release:` only for a maintainer's release pull request.

Do not work directly on `main`, force-push `main`, or mix unrelated changes in
one branch.

## Development

Code is the source of truth. Verify behavior through the active entrypoints and
call sites before changing architecture or documentation.

Keep changes focused and use the smallest relevant validation command. Common
checks include:

The historical test surface is still being audited. Read the
[test-suite maturity notice](docs/testing/TEST_STRATEGY.md#current-test-suite-maturity)
and use the documented curated gates as release evidence; a test outside those
gates must be investigated before it is classified as broken, flaky,
duplicated, or legacy.

```bash
python -m pytest backend/tests -k <pattern>
python -m pytest tests -k <pattern>
npm run check
npm run build
```

Changes to streaming packets or API schemas must keep backend and frontend
contracts synchronized. Runtime side effects must continue through the runtime
provider boundary, and workspace access must remain tenant- and task-scoped.

For behavior changes, prefer a failing test that demonstrates the problem,
then implement the smallest fix. Contributors remain responsible for code and
text produced with AI tools: review it, test it, and disclose substantial
AI-assisted work in the pull request when that context helps review.

## Documentation and Changelog

Update the nearest canonical documentation when a public workflow, API,
configuration value, compatibility promise, or architecture boundary changes.

Add a concise entry under `CHANGELOG.md` → `Unreleased` for a meaningful
user-, contributor-, operator-, or security-visible change. Do not add entries
for formatting, routine tests, internal refactors, or temporary work.

Normal pull requests must not change the product version. Version numbers are
changed once in a dedicated release pull request according to
[RELEASING.md](RELEASING.md).

## Pull Requests

Open a draft pull request when feedback would be useful before the work is
ready. Mark it ready only when it is focused, understandable, and locally
validated.

Every ready pull request must:

- explain the problem, solution, risks, and validation;
- link its issue when one exists;
- include tests for behavior changes when practical;
- update affected documentation and `Unreleased` notes when required;
- contain no credentials, tokens, cookies, private targets, personal data, or
  generated runtime artifacts;
- pass the required GitHub checks;
- resolve review conversations before merge.

Maintainers may close inactive, unsafe, out-of-scope, excessively broad, or
unmaintainable changes. The closure must state the reason or identify the
information needed to continue.

## Review and Merge

Review focuses on correctness, security boundaries, scope, tests,
maintainability, and user impact. Authors should not resolve a substantive
review thread until the concern is addressed or the reviewer agrees it is no
longer applicable.

The repository uses **squash merge** so each pull request becomes one
revertible commit on `main`. A maintainer performs the merge after required
checks pass. Delete the branch after merge. Do not merge while required checks
fail on the assumption that they will be repaired on `main`; fix the same pull
request or open a replacement.

## Licensing

By contributing, you agree that your contributions are licensed under the
repository's [Apache-2.0 license](LICENSE).
