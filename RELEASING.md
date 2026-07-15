# Versioning and Releasing DrowAI

This policy defines DrowAI version numbers, changelog requirements, release
controls, and publication procedure. DrowAI uses
[Semantic Versioning 2.0.0](https://semver.org/spec/v2.0.0.html) and a
human-written [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## Release Unit

A release is an immutable, tested snapshot represented by all of the following:

- a `vMAJOR.MINOR.PATCH` Git tag;
- a GitHub Release published from that tag;
- a matching dated section in `CHANGELOG.md`;
- identical version metadata across the application and package manifests;
- successful release validation recorded in the release pull request.

Commits, merged pull requests, builds, deployments, changelog entries, and
version strings do not independently create a release.

Normal pull requests must not change the product version. User-visible changes
accumulate under `CHANGELOG.md` → `Unreleased`. A dedicated release pull
request assigns the version once for the complete release.

Repository metadata may carry an assigned version while its changes remain
under `Unreleased`. The initial assigned version is `0.1.0`; it is represented
by `v0.1.0` only when the release procedure is complete. This assigned-versus-
published distinction applies to every later version.

## Compatibility Surface

Version decisions consider the documented:

- HTTP, WebSocket, and SSE contracts;
- configuration values and environment variables;
- commands and command-line behavior;
- database migration and upgrade expectations;
- deployment workflows;
- runner and runtime protocols;
- persisted or exported data formats;
- user-visible behavior.

Internal functions and undocumented implementation details are not stable
public APIs. They remain subject to review and testing requirements.

## Version Policy

DrowAI uses `MAJOR.MINOR.PATCH`. The initial versioned release is `0.1.0`.

### Versions below `1.0.0`

| Highest-impact change in the release | Required increment |
| --- | --- |
| Backward-compatible bug, performance, or security fixes only | Patch: `0.1.0` → `0.1.1` |
| Backward-compatible user-visible functionality | Minor: `0.1.1` → `0.2.0` |
| Breaking change to the compatibility surface | Minor: `0.1.1` → `0.2.0`, marked breaking |
| Documentation, tests, or internal maintenance only | No release required |

Although Semantic Versioning permits broad change under major version zero,
DrowAI uses minor increments for features and breaking changes and patch
increments for compatible fixes. Breaking pre-1.0 releases must include
migration guidance.

### Versions from `1.0.0`

- Increment `PATCH` for backward-compatible fixes.
- Increment `MINOR` for backward-compatible functionality or deprecation.
- Increment `MAJOR` for an incompatible compatibility-surface change.

Select the highest increment required by any included change. Reset lower
components to zero when incrementing a higher component.

### Prereleases

DrowAI does not publish public prerelease versions by default. Release
candidates are identified by commit SHA and validated through the release pull
request. Adopting `-alpha`, `-beta`, or `-rc` versions requires a policy
change that defines support, package-format compatibility, and artifact naming
before such tags are published.

## Tag Policy

Release tags use the exact format `vMAJOR.MINOR.PATCH`, for example `v0.4.2`.
They point to the reviewed commit on `main`.

Release tags must never be used as backup markers, moved to a different commit,
overwritten, deleted, or reused. Development checkpoints use commit SHAs or
named development branches.

## Changelog Policy

`CHANGELOG.md` is the user-facing record of notable changes. During
development:

- keep upcoming entries under `## [Unreleased]`;
- use `Added`, `Changed`, `Deprecated`, `Removed`, `Fixed`, or
  `Security`;
- describe observable outcomes rather than file lists, task numbers, or commit
  messages;
- identify breaking changes and required migrations prominently;
- omit formatting, routine test maintenance, and behavior-preserving internal
  refactors.

The release pull request moves applicable entries into a section formatted as:

```text
## [0.4.2] - YYYY-MM-DD
```

Use the actual publication date in `YYYY-MM-DD` format. Retain an empty
`Unreleased` section at the top and maintain comparison links for published
tags. When `Unreleased` names a target version, it must agree with product
metadata and must be removed or replaced when the target becomes a dated
release section.

## Release Procedure

Only a maintainer may execute a release.

1. Review `Unreleased`, included pull requests, and open release blockers.
2. Select the version using the version policy.
3. Create `release/vX.Y.Z` from the latest `main` commit with all required
   checks passing.
4. Update the version in:
   - `pyproject.toml` → `[project].version`;
   - `package.json` → `version`;
   - `package-lock.json` → top-level and root-package `version`;
   - `backend/main.py` → FastAPI `version`.
5. Run `python3 scripts/check_version_consistency.py`.
6. Finalize the dated changelog section and release notes.
7. Complete the release readiness gate in
   [MAINTAINING.md](MAINTAINING.md).
8. Run:

   ```bash
   npm run test:release:main
   npm run test:release:e2e
   ```

9. Confirm **E2E deterministic journeys** passes for the release branch.
10. Manually dispatch **E2E local-runtime certification** with
    `release_certification=true` and link the successful run.
11. Perform the documented clean-install, upgrade, and core user-journey checks
    applicable to the release.
12. Open a pull request titled `release: vX.Y.Z`.
13. Record validation evidence and the lead maintainer's go/no-go decision in
    the pull request.
14. Squash-merge the release pull request. Any required code fix must be made in
    a separate pull request; repeat affected release validation afterward.
15. Update local `main` and create an annotated tag:

    ```bash
    git switch main
    git pull --ff-only
    git tag -a vX.Y.Z -m "DrowAI vX.Y.Z"
    git push origin vX.Y.Z
    ```

16. Publish the GitHub Release from the tag. Use the curated changelog notes,
    identify breaking changes and migrations, and mark it as the latest stable
    release.
17. Verify the release page, source archives, documented installation,
    displayed application version, and every published package or image.

If any content changes after a validation result was recorded, rerun the checks
that could be affected before tagging.

For the initial release, substitute `0.1.0` for `X.Y.Z`: use branch
`release/v0.1.0`, pull request title `release: v0.1.0`, and tag `v0.1.0`.

## Security Releases

Undisclosed vulnerabilities follow [SECURITY.md](SECURITY.md). Prepare the fix,
advisory, changelog entry, and release materials in private. Coordinate
publication so users receive remediation instructions and a fixed version
without exposing reporter details or embargoed information.

Security fixes normally increment `PATCH`. If the remediation introduces a
breaking compatibility change, use the increment required by the version policy
and document the migration.

## Withdrawn Releases

Published tags are immutable even when a release is defective. If a release is
unsafe or unusable:

1. mark the GitHub Release and changelog section as **YANKED** with the reason;
2. publish mitigation, rollback, or upgrade guidance;
3. fix the defect through the normal pull request workflow;
4. publish a new version.

Do not silently replace release artifacts under the same version.

If a credential is published, revoke it immediately. History rewriting is an
exceptional incident-response action requiring an incident record and explicit
lead-maintainer approval; it is not a normal release correction.

## `1.0.0` Criteria

`1.0.0` establishes the stable compatibility baseline. It requires:

- a defined and documented compatibility surface;
- reliable installation, upgrades, and database migrations;
- repeatable supported deployment paths;
- a maintained deprecation and breaking-change process;
- sustainable security reporting and release ownership;
- sufficient operational experience to support the compatibility commitments.

The decision is recorded in an approved release proposal or release pull
request.

## Release Record

The permanent record for each release consists of:

- the immutable Git tag and commit SHA;
- the GitHub Release and release notes;
- the matching `CHANGELOG.md` section;
- the release pull request and validation links;
- artifact identifiers, checksums, and image digests when applicable;
- security advisory references when disclosure permits.

## Policy Changes

Changes to version semantics, tag format, supported release types, or release
controls require a dedicated policy pull request. The change must state whether
it applies prospectively or affects existing release lines.

See [GitHub's release documentation](https://docs.github.com/en/repositories/releasing-projects-on-github/about-releases)
for the relationship between tags, releases, and source archives.
