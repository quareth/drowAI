# Maintaining DrowAI

This handbook defines the repository operating rules for DrowAI maintainers.
It covers change control, pull request decisions, repository configuration,
triage, and release readiness. Version selection and release execution are
defined in [RELEASING.md](RELEASING.md).

The terms **must**, **should**, and **may** indicate mandatory, recommended, and
optional actions respectively. A maintainer who departs from a **should** rule
must document the reason in the relevant issue or pull request.

## Repository Workflow

| Object | Purpose | Changes the released version? |
| --- | --- | --- |
| Issue | Records a bug, proposal, decision, or task | No |
| Branch | Isolates a change from `main` | No |
| Commit | Records a development step | No |
| Pull request | Reviews and validates a proposed change | Normally no |
| Release | Publishes a tested, versioned snapshot | Yes |

Merged changes accumulate under `CHANGELOG.md` → `Unreleased`. A version is
assigned only by a dedicated release pull request. A version string in source
metadata does not by itself constitute a release.

The initial assigned release version is `0.1.0`. The `Unreleased` changelog
section must identify `0.1.0` as its target until the initial release is
finalized. Later release targets follow the version-selection rules in
[RELEASING.md](RELEASING.md).

## Change Control

Every change must follow this sequence:

1. Define the problem, scope, security implications, and verifiable success
   criteria. Substantial changes require an issue before implementation.
2. Create a short-lived topic branch from the latest `main`.
3. Implement one coherent change with the smallest relevant test coverage.
4. Open a pull request using the repository template. Use draft status while
   required work or validation remains.
5. Run the required automated checks and review the complete diff.
6. Address review findings and resolve conversations only when the concern is
   fixed, accepted, or no longer applicable.
7. Squash-merge with the approved pull request title.
8. Delete the merged branch and monitor `main` for regressions.

Direct commits to `main`, force pushes to `main`, and deletion of `main`
are prohibited. An emergency bypass is permitted only when the normal GitHub
workflow is unavailable and delay would materially increase user or security
impact. The maintainer must record the reason, exact changes, validation, and
follow-up work in an issue or pull request.

## Pull Request Acceptance

A pull request may be merged only when:

- its purpose, scope, and user or operator impact are clear;
- it is reviewable and revertible as one unit;
- relevant automated and manual validation passes;
- any skipped validation is justified;
- authentication, authorization, tenant/task isolation, secret handling,
  runtime-provider, and workspace boundaries remain intact;
- public behavior, configuration, compatibility, or architecture changes are
  documented;
- meaningful user-, contributor-, operator-, or security-visible changes have
  an `Unreleased` changelog entry;
- required status checks pass;
- review conversations are resolved;
- the resulting behavior is within the project's supported scope.

Passing automated checks is necessary but does not require a maintainer to
accept an out-of-scope, unsafe, excessively broad, or unsustainable change.
Declined contributions should receive a concise reason.

## Repository Configuration Baseline

Repository administrators must keep GitHub settings aligned with this section.
When a workflow or check name changes, the branch ruleset and this document must
be updated in the same pull request.

### Merge settings

- Squash merge is the only enabled merge method.
- The squash commit title uses the pull request title.
- Merge commits and rebase merges are disabled.
- Head branches are deleted automatically after merge.
- Auto-merge may be enabled only when all branch rules remain enforced.

### `main` branch ruleset

- A pull request is required before merge.
- The release gate and core end-to-end PR check are required status checks.
  Their workflow names are `Release gate / release-gate` and
  `E2E PR core / e2e-pr-core` unless renamed in the repository.
- Review conversations must be resolved.
- Linear history is required.
- Force pushes and branch deletion are blocked.
- Routine administrator bypass is disabled.
- With one maintainer, independent approval is not required because an author
  cannot approve their own pull request.
- With two or more active maintainers, at least one approval from a maintainer
  other than the most recent pusher is required.

A merge queue and strict up-to-date-branch requirement are optional at low
merge volume. They should be enabled when concurrent pull requests regularly
create integration failures or repeated manual rebases.

### Release tags

A tag ruleset must cover `v*`. Authorized maintainers may create release tags,
but published release tags must not be moved, overwritten, or deleted. Tag
names must follow [RELEASING.md](RELEASING.md).

### Security settings

- Private vulnerability reporting is enabled.
- Dependabot alerts and security updates are enabled.
- Secret scanning and push protection are enabled when supported by the hosting
  plan.
- Security scanners are assigned an owner and reviewed regularly. A scanner
  must not remain enabled if its results are routinely ignored.
- Repository credentials and publishing permissions follow least privilege.

GitHub Issues are reserved for reproducible defects and focused feature
proposals submitted through the configured forms. Vulnerability details remain
private and follow [SECURITY.md](SECURITY.md). If a community discussion channel
is introduced later, its repository setting and issue-template contact link
must be updated together.

## Issue and Pull Request Triage

Use a limited, consistent label set:

- type: `bug`, `feature`, `documentation`, `security`, `maintenance`;
- status: `needs-triage`, `needs-info`, `blocked`, `ready`;
- impact: `breaking`, `release-blocker`;
- contribution: `good first issue`, `help wanted`.

Labels referenced by issue forms must exist in the repository. Label changes
must update this section and the affected forms together.

Maintainers should review new issues, pull requests, dependency alerts, security
alerts, and failed scheduled CI on a regular cadence. Triage must:

- identify duplicates and link the canonical item;
- request a minimal reproduction or missing decision context;
- distinguish support questions from actionable defects;
- keep sensitive security reports out of public threads;
- assign `release-blocker` only to issues that prevent a supported release;
- remove `release-blocker` only after verification;
- close inactive items only after stating what information or action is needed.

The project does not promise response times unless a separate published service
commitment says otherwise.

## Release Decision

Maintainers release when the accumulated changes form a useful, supportable,
and validated snapshot. A release may be triggered by:

- a coherent set of user-visible changes;
- a backward-compatible fix that users need;
- a coordinated security fix;
- a compatibility or deployment milestone.

No fixed calendar requires a release. A regular release review may be scheduled,
but readiness controls must not be waived to meet a date.

## Release Readiness Gate

This gate applies to every stable release. The release owner records evidence
in the release pull request. A required item that is unknown or unverified is a
release blocker unless the limitation is explicitly documented and accepted by
the lead maintainer.

### Product and compatibility

- Supported operating systems, runtime dependencies, deployment paths, and
  upgrade assumptions are documented.
- A clean installation succeeds using only the documented procedure.
- Core user journeys pass on each supported deployment path affected by the
  release.
- Database migrations, restart behavior, and rollback or recovery instructions
  are verified when relevant.
- Breaking changes, deprecations, and migration steps are explicit.
- Known limitations are documented.

### Security and legal

- Release changes contain no credentials, tokens, private targets, personal
  data, generated task artifacts, local databases, or sensitive logs.
- Authentication, authorization, tenant/task isolation, runtime-provider
  boundaries, workspace access, and exposed network defaults are reviewed when
  affected.
- Dependencies and distributable assets have compatible licenses and required
  attribution.
- Private vulnerability reporting is operational.
- Any coordinated vulnerability disclosure is approved for publication.
- The initial release additionally requires a scan of reachable repository
  history and a complete third-party asset/license inventory.

### Repository and documentation

- Required branch and tag rulesets are active.
- Required CI and release certification checks pass on the release commit.
- No open `release-blocker` remains.
- Version metadata passes
  `python3 scripts/check_version_consistency.py`.
- The target version recorded under `CHANGELOG.md` → `Unreleased` agrees
  with the assigned version until that target is converted into a dated release
  section.
- `CHANGELOG.md`, README, installation instructions, support policy, security
  policy, and affected architecture documentation match the release behavior.
- Issue and pull request templates remain valid.

### Release materials

- The release pull request contains only version metadata, changelog changes,
  release notes, and release-specific packaging metadata.
- Release notes identify user-visible changes, breaking changes, migrations,
  security notices, and known limitations.
- Published artifact names, image tags, and digests are recorded when
  applicable.
- The lead maintainer records an explicit go/no-go decision.

Release execution follows [RELEASING.md](RELEASING.md).

## Regressions on `main`

When a merged change causes a material regression:

1. stop dependent merges when further changes could compound the failure;
2. revert the offending pull request unless a verified forward fix is equally
   small and safer;
3. restore required checks;
4. investigate and fix the root cause on a topic branch;
5. add regression coverage before reapplying the change.

Do not merge an unverified repair directly into `main`.

## Policy Maintenance

Policy changes use the same pull request workflow as code. The pull request must
explain the reason, compatibility impact, affected roles, and effective point.

## References

- [Semantic Versioning 2.0.0](https://semver.org/spec/v2.0.0.html)
- [Keep a Changelog 1.1.0](https://keepachangelog.com/en/1.1.0/)
- [GitHub rulesets](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-rulesets/about-rulesets)
- [GitHub protected branches](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-protected-branches/about-protected-branches)
- [GitHub community profiles](https://docs.github.com/en/communities/setting-up-your-project-for-healthy-contributions/about-community-profiles-for-public-repositories)
