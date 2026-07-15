---
id: ffuf_crawler
name: ffuf-crawler
type: tool
version: 1
description: Guide parameter generation for ffuf crawler-mode path and content discovery.
trigger_tool_ids:
  - web_applications.web_crawlers.ffuf
stages:
  - tool_parameters
---

# FFUF Crawler

Use this runbook only after `web_applications.web_crawlers.ffuf` has already been selected. This tool discovers URL path candidates under an in-scope HTTP(S) target: directories, files, panels, backups, static resources, API route names, and path-shaped object IDs.

The `FUZZ` marker must be in the URL path. Do not place `FUZZ` in headers, cookies, request bodies, query parameters, credentials, IDs outside the path, virtual hosts, or request bodies for this crawler variant.

## Target Template

- Preserve the exact scheme, host, port, and known base path from the user request or prior context.
- Use the most specific known application path. For observed `http://host/data/1`, use `http://host/data/FUZZ`; for observed `https://host/app/login`, use `https://host/app/FUZZ`.
- Do not drop a known base path, switch protocol, change ports, fuzz the host, or invent paths, authentication, proxies, or wordlist locations.

## Candidate Source

- Choose the smallest candidate source that matches the hypothesis.
- Use `generated_sequence` when the path part is numeric, sequential, or pattern-like, such as `/data/FUZZ`, `/download/FUZZ`, or `/users/FUZZ/profile`.
- For numeric path/object enumeration, include `0` by default unless excluded, include known observed values as controls, use bounded ranges such as `0-50` or `0-100` when no range is specified, avoid recursion, and avoid extensions unless the observed path is file-like.
- Use `inline_values` when candidates are a small context-derived set: page links, robots.txt entries, sitemap entries, JavaScript routes, API words, user-provided lists, or app-specific words like `capture`, `report`, `export`, `backup`, `upload`, and `admin`.
- Use installed wordlists for broad black-box hidden path discovery, common directories/files/panels/endpoints, or when no better context-derived candidate set exists.

Allowed installed wordlists:

- `/usr/share/dirb/wordlists/common.txt`
- `/usr/share/seclists/Discovery/Web-Content/common.txt`
- `/usr/share/seclists/Discovery/Web-Content/common_directories.txt`
- `/usr/share/seclists/Discovery/Web-Content/raft-small-words.txt`
- `/usr/share/wfuzz/wordlist/general/common.txt`

Do not use `/usr/share/wordlists/rockyou.txt.gz` for path discovery.

## Wordlists And Extensions

- Use `/usr/share/dirb/wordlists/common.txt` for cautious first-pass discovery, quick checks, fragile targets, or light/common scans.
- Use `/usr/share/seclists/Discovery/Web-Content/common.txt` for normal common web content discovery.
- Use `/usr/share/seclists/Discovery/Web-Content/common_directories.txt` for directory-only discovery.
- Use `/usr/share/seclists/Discovery/Web-Content/raft-small-words.txt` when appending extensions or looking for file/route name stems.
- Use `/usr/share/wfuzz/wordlist/general/common.txt` only as an alternate small/common fallback.
- Use `append_extensions` only for file discovery, backup discovery, or technology-specific file discovery. Do not append extensions for numeric IDs, directory-only discovery, extensionless routes, API route-name discovery, or already complete filenames.
- Keep extension sets small. Do not combine many extensions with large wordlists unless the user asks for thorough discovery.

## Response Strategy

- For normal path discovery, prefer useful statuses: `200-299`, `301`, `302`, `307`, `308`, `401`, `403`, and `405`.
- Include `500-599` only for small bounded route discovery, behavioral comparison, or lab/CTF contexts where backend errors may identify real paths.
- For small numeric/object enumeration, broad matching such as `200-599` is acceptable because status, size, and redirect differences are often the signal.
- Do not filter `401`, `403`, or redirects by default; they can indicate valid protected resources or canonical paths.
- Use calibration when invalid paths may look successful: wildcard 200s, soft-404 pages, SPAs returning one page, login redirects for every invalid path, CDN/object fallback behavior, or broad wordlist scans.
- Leave calibration off for small bounded candidate sets, raw status/size comparison, numeric path enumeration, and cases where calibration may hide unusual differences.
- Use size, word, line, or status filters only when context provides a known bad-response baseline. Do not guess filters.

## Recursion And Runtime

- Enable recursion only for directory-like discovery when requested or clearly justified. Keep depth explicit: `1` for cautious discovery, `2` for normal lab/deeper discovery, and more than `2` only when explicitly requested or clearly allowed.
- Do not enable recursion for numeric IDs, object paths, user/ticket/report/capture IDs, API route-name discovery, or file discovery with extensions.
- Keep `follow_redirects=false` by default because redirect status and location are useful evidence.
- Keep `ignore_body=false` when response size, words, lines, or content-derived comparison matters. Use `ignore_body=true` only for status/redirect-only checks, large responses, or large candidate sets where body download is unnecessary.
- For lab/CTF/local targets, threads around `20` are usually reasonable. For production-like, fragile, remote, or rate-limited targets, use lower threads, rate limits, or delay.
- Always bound runtime for large wordlists, recursion, extension-heavy discovery, or uncertain targets.

## Output Behavior

- Do not assume the wrapper adds silent mode, JSON output, or artifact output.
- Choose `silent=true` only when concise matched-payload stdout is desired.
- Choose `json_output_path` only when structured status, size, word, line, URL, and redirect analysis is needed. Use a workspace-relative path such as `artifacts/ffuf_crawler.json`.
- Leave `silent=false` and `json_output_path` unset when normal ffuf console output is useful for debugging or human-readable review.

## Advanced And Authentication

- Keep `http2=false` unless prior context shows HTTP/2 is required or requested.
- Set `ignore_tls=true` only for HTTPS targets with known certificate issues, self-signed lab certificates, or explicit user direction.
- Use `proxy` and `replay_proxy` only when provided by the user, runtime context, or engagement configuration.
- Keep `debug_log=false` unless debugging ffuf wrapper or parameter construction.
- Keep `match_output_dir` unset unless the user or runtime explicitly asks to save matched request/response material separately.
- Use cookies, headers, bearer tokens, basic auth, and other authentication parameters only when explicitly provided by the user, prior context, or engagement configuration.

## Builder Intent

- `_builder_intent` should describe the parameter purpose, not predict a vulnerability.
- Good: `Enumerate numeric path IDs under /data with a bounded generated range and compare status/size differences.`
- Good: `Run cautious common-path discovery under /app using an installed small web wordlist.`
- Bad: `Exploit IDOR.`
- Bad: `Dump sensitive data.`
