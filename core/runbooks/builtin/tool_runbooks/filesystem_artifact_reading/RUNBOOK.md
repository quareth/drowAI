---
id: filesystem_artifact_reading
name: artifact-evidence-reading
type: tool
version: 1
description: Guide parameter generation for already-selected filesystem read/search tools so saved artifact evidence is retrieved without dumping large files.
trigger_tool_ids:
  - filesystem.read_file
  - filesystem.search_text
stages:
  - tool_parameters
---

# Artifact Evidence Reading

Use this runbook only after a filesystem read/search tool has already been selected. Its job is not to decide whether artifact reading should happen; its job is to configure the selected tool so artifact evidence is retrieved with bounded, useful parameters.

## Parameter Safety Rules

- If artifact metadata says the file is missing, unavailable, or outside the workspace, do not invent facts from that artifact.
- Full-file reads are allowed only when metadata shows the file is small: <= 200 lines and <= 32768 bytes.
- For medium or large files, use search or bounded reads before any wider read.
- Omit `max_bytes` and `start_byte` for normal text evidence reads. They are byte/full-read safety controls, not an evidence retrieval strategy.
- Leave transport unset unless the user explicitly needs visible PTY behavior.

## Workflow

1. Inspect the available artifact metadata: path, status, size_bytes, and line_count.
2. Identify the exact evidence needed, such as an IP, host state, port, service, path, status code, recovered value, or error.
3. Prefer filesystem.search_text when there is a known literal, regex, field name, status marker, or protocol marker to test for.
4. When the user names one artifact file, operate on that file path. Search an artifact directory only when the task is to discover which artifact contains evidence.
5. Use filesystem.read_file with explicit read_mode="grep", read_mode="head", read_mode="tail", or read_mode="range" when reading a single artifact directly.
6. If using filesystem.read_file for pattern extraction, set `read_mode="grep"` and `grep_pattern` explicitly rather than relying on the simplified `search` alias.
7. Use read_mode="head" only to understand an unknown file format, then switch to search or a narrow range.
8. If prior output already contains candidate line numbers, read a narrow range around those lines only when nearby context is needed.
9. If the artifact is small and the user asks for proof, citations, or line numbers, use one range read over the known line_count with include_line_numbers=true.
10. Configure the narrowest call that can confirm presence, confirm absence, or expose the next useful slice of evidence; a no-match result is valid evidence.
11. The post-tool reasoning step will decide what to do after the result.

## Artifact Playbooks

- Nmap XML or text: search for address fields, host state, port ids, state attributes, service names, banners, and summary lines. For XML attributes, prefer stable markers such as `addr=`, `portid=`, `state=`, `service name=`, and `conf=`.
- Gobuster or ffuf output: search for result arrays, path/url fields, status codes, redirect locations, content length, and non-empty findings.
- Hashcat output: search for recovered hashes, cracked values, session/status markers, potfile-style separators, and success/failure summary lines.
- Tcpdump text: search for protocol names, IPs, ports, flags, DNS names, HTTP markers, and timestamps; read narrow ranges around matching packets.
- Generic logs: use tail for latest-result questions, head for format discovery, and search for concrete markers before range reads.

## Bad Calls

- Do not call filesystem.read_file with read_mode="full" when size or line count is unknown or large.
- Do not increase max_bytes because you are uncertain.
- Do not include max_bytes on grep, head, tail, or range reads.
- Do not read a whole artifact to find one IP, port, URL path, status code, or recovered value.
- Do not search `/workspace/artifacts` or `artifacts/` as a directory when the user named a specific artifact file.
- Do not force PTY transport for normal evidence extraction.
- Do not encode a multi-step reading plan in one call; configure only the selected tool call for this iteration.
