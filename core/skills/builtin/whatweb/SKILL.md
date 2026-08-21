---
name: whatweb
description: Operate WhatWeb for bounded web technology fingerprinting, plugin selection, evidence collection, and controlled follow-up requests.
compatibility: WhatWeb v0.6.4
metadata:
  version: "1"
  activation: "selectable"
  agent-ids: "webweaver"
---

# WhatWeb CLI Guidance

WhatWeb identifies web servers, frameworks, content-management systems,
JavaScript libraries, and other technologies through plugin signatures. Its
matches are fingerprint evidence, not vulnerability findings.

## Command shape

```sh
whatweb [options] 'https://approved.example'
```

WhatWeb accepts URLs, hostnames, files, ranges, and CIDRs, but use only the
exact URL or host assigned. Do not derive ranges, sibling hosts, alternate
schemes, or discovered domains unless they are explicitly in scope.

## High-signal parameters

### Request intensity

- `-a 1`: stealth mode and the default. It normally makes one request per
  target and follows redirects.
- `-a 3`: after a level-1 match, matching plugins may make extra requests to
  improve identification or version confidence.
- `-a 4`: tries URLs from all plugins and can make many requests. Avoid it for
  routine reconnaissance.

Begin at level 1. Use level 3 only when ambiguity matters to the objective and
the additional requests remain within the assignment. Do not use level 4 by
default.

### Plugins and matching

- `-l`: list available plugins.
- `-I [term]`: show plugin information, optionally filtered by search terms.
- `-p <list>`: select a comma-separated plugin set. Prefix entries with `+` or
  `-` to add or remove them from the default set.
- `-g <text|regex>`: display only results matching a string or regular
  expression.

Leave plugin selection at its default for broad fingerprinting. Restrict `-p`
when the assignment asks about a particular technology or when a narrow plugin
set reduces unnecessary follow-up requests.

### HTTP behavior

- `-U <value>`: set the User-Agent.
- `-H 'Name: value'`: set a request header. A named default header is replaced.
- `--follow-redirect never|http-only|meta-only|same-site|always`: control which
  redirects are followed. The program default is `always`; use `same-site` for
  bounded reconnaissance unless the assignment requires another behavior.
- `--max-redirects <n>`: cap redirect hops.
- `--proxy <host:port>`: route requests through a configured proxy.
- `--no-cookies`: disable automatic cookie handling when session continuity is
  unnecessary.
- `--open-timeout <seconds>` / `--read-timeout <seconds>`: bound connection and
  response waits.

Authentication and cookie options expose sensitive values when supplied on a
command line. Do not place credentials, bearer tokens, or session cookies in
arguments. Use them only through an approved credential-delivery mechanism.

### Performance and output

- `-t <n>`: simultaneous threads. The default is 25; use a lower value for one
  target or a fragile service.
- `--wait <seconds>`: delay between connections; most useful with one thread.
- `--colour=never`: remove terminal colour sequences.
- `-q`: suppress brief screen output when a log format supplies the evidence.
- `--no-errors`: suppress routine error text. Omit it while diagnosing failures.
- `--log-brief=<file>`: write compact one-line findings.
- `--log-json=<file>`: write structured findings. `-` directs output to stdout.
- `--log-json-verbose=<file>`: write more detailed JSON at a higher output cost.
- `--output-sync`: flush output immediately, trading throughput for visibility.

Prefer brief output for a quick single-target fingerprint and JSON when results
must be parsed or retained as structured evidence.

## Task-based patterns

Low-impact fingerprinting:

```sh
whatweb --colour=never --no-errors -a 1 --follow-redirect=same-site \
  --max-redirects=5 -t 5 --open-timeout=10 --read-timeout=20 \
  'https://approved.example'
```

Structured output for evidence processing:

```sh
whatweb --colour=never --quiet --no-errors -a 1 \
  --follow-redirect=same-site --max-redirects=5 -t 5 \
  --open-timeout=10 --read-timeout=20 --log-json=- \
  'https://approved.example'
```

Narrow plugin follow-up after an ambiguous initial result:

```sh
whatweb --colour=never --no-errors -a 3 -p 'WordPress' \
  --follow-redirect=same-site --max-redirects=5 -t 3 \
  --open-timeout=10 --read-timeout=20 'https://approved.example'
```

Replace plugin names with those supported by the installed version and relevant
to the observed evidence. These patterns illustrate composition and are not
mandatory command lines.

## Evidence interpretation

- Record the effective target, redirect behavior, plugin names, matched strings,
  reported versions, and uncertainty.
- Treat version results as claims supported by plugin signatures. Missing or
  conflicting signals should remain explicitly uncertain.
- A missing match does not prove a technology is absent, and a plugin match does
  not prove that the technology is vulnerable.
- Return concise fingerprints and their supporting signals. Do not turn detected
  software into an exploitation task.

## Failure recovery

- Timeout or instability: reduce threads, keep the target fixed, and adjust
  connection or read timeouts conservatively.
- Redirect loops or cross-site movement: lower `--max-redirects` or use
  `--follow-redirect=never` while recording the limitation.
- Excessive requests: return to aggression level 1 and narrow the plugin set.
- Noisy output: disable colour and select brief or JSON logging rather than
  increasing verbosity.
- Empty results: verify the exact target and scheme, rerun at level 1 without
  suppressed errors, and report connection or fingerprint limitations.
