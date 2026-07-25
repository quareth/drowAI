# Mechanical validation scorecard

The durable report is JSON. Evidence references may point to ignored local
reports/screenshots but must not contain credentials, cookies, tokens, private
targets, or full sensitive output.

## Required sections

| Section | Required mechanics |
|---|---|
| `model` | exact NVIDIA preset/model, existing connection marker |
| `safe_target` | loopback/reserved target source and local resolver when DNS |
| `schema_runs` | minimal and full parameter execution |
| `cases` | success, empty, partial/timeout, actual failure; applicability explicit |
| `compression` | exact total/shown/omitted, deterministic result, marker budget |
| `artifacts` | expected vs observed and secret-safe reference |
| `knowledge` | expected vs observed task-scoped facts |
| `gui` | selected tool, preserved params, rendered result, attempt count |
| `documentation` | current branch guide/runbook checks and any corrections |
| `cleanup` | task/runtime deletion and stack ownership |

## Allowed targets

- `127.0.0.1` or another address inside `127.0.0.0/8`
- `localhost`
- an HTTP(S) URL whose host is loopback or `localhost`
- a reserved name ending in `.test`, `.invalid`, or `.localhost`
- loopback ranges/subnets

`example.com`, public domains, private third-party domains, and inferred
fallback targets are not allowed in real execution. A local DNS fixture should
use `drowai.test` with a resolver on loopback.

## Classification

- Schema absence or parameter loss is `FAIL`, never `INCONCLUSIVE`.
- `INCONCLUSIVE` is reserved for selected-model tool-choice uncertainty after
  two attempts while direct Kali/schema mechanics already pass.
- Unknown or failed cleanup is `NEEDS_CLEANUP`.
- `PASS` requires all required cases and gates.

The report must set `mechanics_only: true` and must not contain
`prompt_quality`, `prose_quality`, `answer_quality`, or equivalent scoring.

## Secret handling

Only `<KEY_SET>` and `<NO_KEY>` may represent credential presence, and the
mechanical validator must reject any report containing bearer-token or common
API-key shapes without echoing the sensitive value.
