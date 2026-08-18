---
name: katana
description: Operate Katana for bounded web crawling, JavaScript endpoint extraction, known-file discovery, and structured endpoint collection.
compatibility: Katana v1.7.x
metadata:
  version: "1"
  activation: "selectable"
  agent-ids: "webweaver"
---

# Katana CLI Guidance

Katana crawls web applications and emits discovered endpoints. It is useful for
link traversal, JavaScript-derived routes, forms, robots files, and sitemaps. It
discovers attack surface; a discovered endpoint is not proof of accessibility
or vulnerability.

## Command shape

```sh
katana -u 'https://approved.example' [scope] [discovery] [limits] [output]
```

Use `-u` for one or more comma-separated URLs and `-list <file>` for a target
file. Prefer one exact assignment URL. Do not expand a host into sibling hosts,
subdomains, IP ranges, or external domains unless the assignment allows it.

## High-signal parameters

### Crawl behavior

- `-d <n>`: maximum link depth. The default is 3.
- `-s depth-first|breadth-first`: visit strategy. Depth-first is the default;
  breadth-first gives broader early coverage.
- `-jc`: parse and crawl endpoints found in JavaScript.
- `-jsl`: apply deeper JavaScript parsing. It is memory intensive; reserve it
  for narrow targets where ordinary JavaScript crawling is insufficient.
- `-kf robotstxt|sitemapxml|all`: crawl known files. Use depth 3 or greater so
  their links can be followed correctly.
- `-fx`: include discovered forms and fields in JSONL output without submitting
  them.
- `-iqp`: treat URLs with the same path but different query values as one crawl
  candidate when parameter-value variation is creating noise.
- `-fsu`: collapse structurally similar URLs such as repeated numeric paths.
- `-dr`: do not follow redirects.

### Scope

- `-fs fqdn`: restrict crawling to the exact input host. Use this as the normal
  assignment boundary.
- `-fs rdn`: include the registered domain and its subdomains. Use only when
  that wider scope is explicitly assigned.
- `-cs <regex>`: follow only URLs matching an allow-pattern.
- `-cos <regex>`: exclude URLs matching a deny-pattern, such as logout paths.
- `-e <value>`: exclude matching hosts, CIDRs, IPs, or patterns.
- `-ns`: disables host-based scope. Do not use it for bounded assessments.

Scope controls what Katana follows. Output filters only change what is printed;
they do not narrow the crawl boundary.

### Resource controls

- `-ct <duration>`: maximum crawl duration, such as `90s`, `5m`, or `1h`.
- `-mdp <n>`: maximum pages per domain. Set it because the default is unlimited.
- `-c <n>`: concurrent fetchers for each target.
- `-p <n>`: input targets processed in parallel. Use `1` for one assigned URL.
- `-rl <n>` / `-rlm <n>`: global requests per second or per minute.
- `-hrl <n>` / `-hrlm <n>`: per-host requests per second or per minute.
- `-rd <seconds>`: delay between requests.
- `-timeout <seconds>`: request timeout.
- `-retry <n>`: request retry count.
- `-mrs <bytes>`: maximum response bytes read.

Always bound duration, pages, concurrency, rate, timeout, and retries. Start
conservatively and increase only when the assigned target tolerates it.

### Filtering and output

- `-ef <extensions>`: omit static extensions such as images and fonts.
- `-em <extensions>`: emit only selected extensions; include `none` to retain
  extensionless URLs.
- `-mr <regex>` / `-fr <regex>`: include or exclude matching output URLs.
- `-silent`: print findings without banners or progress messages.
- `-j`: emit JSONL with discovery context.
- `-or` / `-ob`: omit raw request-response data and response bodies from JSONL.
- `-o <file>`: write output to a file; create its parent directory first.
- `-ot <template>`: format selected output fields. Prefer it over deprecated
  `-f` for new workflows.

Use plain output for an endpoint list. Use JSONL only when source, form, or
request context is needed, normally with `-or -ob` to control volume.

## Task-based patterns

Focused same-host crawl:

```sh
katana -u 'https://approved.example' -fs fqdn -d 3 -ct 3m -mdp 500 \
  -c 5 -p 1 -rl 20 -timeout 10 -retry 1 -fsu -silent
```

JavaScript endpoint discovery on a narrow target:

```sh
katana -u 'https://approved.example/app/' -fs fqdn -d 3 -jc -ct 5m \
  -mdp 750 -c 5 -p 1 -rl 20 -timeout 10 -retry 1 -fsu -silent
```

Known-file and form evidence in compact JSONL:

```sh
mkdir -p crawl
katana -u 'https://approved.example' -fs fqdn -d 3 -kf robotstxt -fx \
  -ct 3m -mdp 500 -c 5 -p 1 -rl 20 -timeout 10 -retry 1 \
  -j -or -ob -silent -o crawl/katana.jsonl
```

Choose only the flags required for the objective. These patterns illustrate
composition and are not mandatory command lines.

## Safety and evidence

- Do not enable automatic form filling, auto-login, secret validation, or
  unrestricted crawling unless the assignment explicitly requires and permits
  that behavior.
- Do not place credentials, cookies, tokens, or other secrets directly in a
  command line or retained output.
- The current runtime does not include browser dependencies for headless mode;
  use standard crawling behavior.
- Deduplicate results and report unique in-scope URLs, forms, script-derived
  routes, robots or sitemap discoveries, and crawl limitations.
- Do not automatically authenticate, fuzz, or exploit discovered endpoints.

## Failure recovery

- Excessive volume: tighten `-fs`, add `-cs` or `-cos`, lower `-mdp`, `-d`, or
  `-ct`, enable `-fsu`, and filter irrelevant extensions.
- Rate limiting or instability: lower `-c` and `-rl`, add `-rd`, and keep retries
  low.
- High memory use: disable `-jsl`, lower concurrency, and reduce response size.
- Thin client-rendered results: report the limitation when standard crawling
  cannot observe the required routes; do not enable unavailable dependencies.
