# Security Policy

## Supported Versions

Security fixes target the latest stable GitHub Release. Other versions receive
fixes only when a published advisory explicitly extends support.

| Version or source state | Security support |
| --- | --- |
| Latest stable GitHub Release | Supported on a best-effort basis |
| Older stable releases | Unsupported unless an advisory states otherwise |
| Prereleases and development branches | Unsupported |
| Historical or non-product tags | Unsupported |

`main` may contain the code used to prepare a fix, but it is not a supported
user release. In particular, `0.1.0` in application or package metadata is the
initial assigned version; security support begins when `v0.1.0` is published
as a stable GitHub Release.

## Reporting a Vulnerability

Do not disclose suspected vulnerabilities in a public issue, discussion, pull
request, log bundle, or social channel.

Use GitHub private vulnerability reporting as the primary channel. If that
channel is unavailable, contact the repository owner through the private
contact method on their [GitHub profile](https://github.com/quareth) and request
a secure reporting channel before sending sensitive details.

A useful report includes:

- the affected release, component, and commit when known;
- prerequisites and expected impact;
- minimal reproduction steps or a proof of concept;
- whether credentials, authorization, tenant boundaries, task isolation,
  workspace access, runtime execution, or data exposure are involved;
- suggested mitigation when available.

Encrypt or omit secrets, personal data, private targets, and customer
information. Do not access systems or data without explicit authorization.

## Response Process

Maintainers will:

1. acknowledge the report when maintainer availability permits;
2. assess reproducibility, impact, and affected versions;
3. coordinate remediation and release planning privately;
4. agree on disclosure timing with the reporter when practical;
5. publish an advisory and fixed version when disclosure is appropriate.

Reporter identity and nonessential sensitive details will not be disclosed
without permission. The project does not provide a formal response-time SLA or
bug-bounty program unless one is announced separately.

## Disclosure and Releases

Security fixes follow [RELEASING.md](RELEASING.md). Advisories must identify
affected versions, fixed versions, impact, mitigation, and upgrade guidance.
Embargoed details remain private until the coordinated publication point.

Public acknowledgments are offered when requested and when they do not expose
sensitive or legally restricted information.
