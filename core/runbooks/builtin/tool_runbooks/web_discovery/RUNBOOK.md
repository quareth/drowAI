---
id: web_discovery
name: web-discovery
type: tool
version: 1
description: Guide web tool selection and parameter generation for visible HTTP probing, download, and path discovery tools.
trigger_tool_ids:
  - information_gathering.web_enumeration.http_request
  - information_gathering.web_enumeration.http_download
trigger_category_ids:
  - web_applications
stages:
  - tool_selection
  - tool_parameters
---

# Web Discovery

Use this runbook when the current task involves probing known HTTP URLs, downloading known web resources, or discovering web paths with the model-visible web tool catalog.

This runbook is scoped to the visible web-discovery tools in the current model-facing catalog.

This runbook does not authorize scanning. Preserve the user-provided target, scope, protocol, port, rate limits, authentication boundaries, and engagement constraints.

## Stage: tool_selection

Choose tools by the shape of the work, not by the fact that every option speaks HTTP.

### Tool Families

- `information_gathering.web_enumeration.http_request`: use for one known URL, or a small explicit set of known URLs, when the task is status, headers, redirects, body preview, cookies, authentication behavior, or validation of a specific finding.
- `information_gathering.web_enumeration.http_download`: use only when the task is to save one known HTTP(S) resource into the workspace.
- `web_applications.web_crawlers.ffuf`: use when the task is to discover many unknown web paths, directories, files, panels, backup files, API routes, or endpoints from a wordlist-shaped search.

### Selection Rules

1. If the user asks to find, discover, enumerate, brute force, fuzz, crawl, or try many possible web paths, select `web_applications.web_crawlers.ffuf` rather than only `http_request`.
2. If the user gives one exact URL or a small explicit list of exact paths to inspect, select `http_request`.
3. If the user asks to download a known resource, select `http_download`; do not use download tools to discover unknown paths.
4. If prior evidence already identifies a base URL, preserve that scheme, host, port, and base path when selecting web tools.
5. If the immediate task is not web probing, web resource download, or web path discovery, this runbook should not drive tool selection.

### Selection Anti-Patterns

- Do not satisfy broad web path discovery by making two or three guessed `http_request` calls.
- Do not select `http_download` unless the requested outcome is a saved file.
- Do not use this runbook to select non-web tools.

## Stage: tool_parameters

Configure only the selected tool call for the current iteration. Do not encode future contingencies or a multi-step web plan into one call.

### Shared Parameter Rules

- Preserve the exact target, scheme, host, port, and base path from scope, prior evidence, or the active directive.
- Do not silently switch HTTP to HTTPS, HTTPS to HTTP, or one port to another.
- Do not invent authentication headers, cookies, request bodies, alternate hosts, virtual hosts, or broader path prefixes.
- Keep timeouts, delays, rates, recursion, and output capture bounded by the task constraints.
- Prefer first-class visible tools over shell or raw curl commands when the catalog exposes the needed operation.

### HTTP Request

- Use `http_request` for concrete URL inspection and validation.
- Capture body content only when body content is needed; status, headers, redirects, and content type are often enough.
- Follow redirects only when the task asks for redirect behavior or effective URL validation.
- Do not turn broad path enumeration into guessed repeated requests unless the paths are already explicit and small in number.

### HTTP Download

- Use `http_download` only with a concrete known file URL.
- Write to a workspace-relative destination.
- Use checksum, size, resume, or overwrite controls only when required by the task or prior state.
- Do not download speculative path candidates.

### FFUF Crawler

- Use `web_applications.web_crawlers.ffuf` for path and directory discovery.
- Keep crawler parameter construction in the dedicated `ffuf_crawler` tool runbook.

### Result Signals

- Path discovery success signals include non-empty findings, URL or path, HTTP status, redirect location, content length, word/line counts, and calibration or wildcard indicators.
- HTTP request success signals include status code, effective URL, redirect chain, headers, content type, body preview, saved artifacts, and TLS/auth/session errors.
- A no-result outcome can be valid evidence when the selected tool and parameters match the requested scope.
