---
id: tshark_pcap_analysis
name: tshark-pcap-analysis
type: tool
version: 2
description: Guide parameter generation for bounded TShark intent profiles and PCAP evidence extraction.
trigger_tool_ids:
  - sniffing_spoofing.network_sniffers.tshark
stages:
  - tool_parameters
---

# TShark PCAP Analysis

Use this runbook only after `sniffing_spoofing.network_sniffers.tshark` has already been selected.

The tool accepts structured parameters only. Do not generate raw `tshark` commands, CLI flags, or shell snippets.

## Core Rule

Choose one bounded `analysis_mode` intent for the current call:

- `survey`
- `anomaly_detection`
- `investigate_protocol`
- `extract_evidence`
- `find_security_relevant_artifacts`

The TShark tool deterministically maps the intent and parameters to a bounded field query. Prefer a narrower intent or pivot over increasing `max_rows`.

## Required Inputs

- Use `input_file` for an existing PCAP artifact.
- Use `interface` only for live capture.
- Keep `input_file` workspace-relative.
- Do not invent paths, interfaces, hosts, ports, protocols, streams, frames, filters, terms, or fields.
- Do not set `capture_filter` when `input_file` is set.
- Use `display_filter` only when the current request already provides a precise filter or pivot.

## Intent Selection

### `survey`

Use for unknown, broad, exploratory, or first-pass PCAP analysis.

Choose this when the user asks what is inside a PCAP, whether it has useful traffic, which hosts/protocols appear, or when there is no known protocol or evidence pivot.

Treat survey as the routing step, not the final evidence step. Read the returned `services`, `interesting_streams`, and `recommended_next_queries` metadata, then choose the next intent from those hints.

Survey is expected to identify passive services, ports, streams, error hints, cleartext auth command hints, and protocol directions without extracting credential values as proof.

Recommended parameters:

- `analysis_mode`: `survey`
- `max_rows`: `100`
- `include_payload_indicators`: `false`
- `sensitive_proof_mode`: `proof_excerpt`

### `anomaly_detection`

Use when the user asks for suspicious traffic patterns, failures, retransmissions, resets, protocol errors, DNS errors, HTTP errors, TLS alerts, or signs that traffic is abnormal.

Do not use this as the default for unknown PCAPs; use `survey` first unless the current request is specifically about anomalies.

### `investigate_protocol`

Use when the user names a protocol or a prior result identifies one worth inspecting.

Required:

- `analysis_mode`: `investigate_protocol`
- `protocol`: a simple protocol name such as `http`, `dns`, `tls`, `ftp`, `smtp`, `pop`, or `imap`

Optional narrowing:

- `host`
- `port`
- `display_filter`
- `max_rows`

Do not use this mode without a protocol.

### `extract_evidence`

Use when there is a concrete pivot and the current request needs packet-level evidence.

At least one pivot is required:

- `stream_id`
- `frame_number`
- `frame_start` / `frame_end`
- `host`
- `port`
- `display_filter`

Use `fields` only with this mode, and only for allowlisted fields. Do not use this for broad exploration.

Do not request non-allowlisted fields.

### `find_security_relevant_artifacts`

Use when the user asks for credentials, passwords, tokens, API keys, cookies, authorization headers, authentication material, or pentest evidence.

Optional:

- `terms`: bounded search terms when the user supplied specific words.
- `host`, `port`, or `display_filter` when already known.
- `sensitive_proof_mode`.

Set `sensitive_proof_mode` deliberately:

- `metadata_only`: classify without exposing reusable values.
- `proof_excerpt`: return bounded proof excerpts when needed.
- `fingerprint`: correlate durable findings without storing reusable values.

## Filters And Limits

- Use `host` only for IP address filtering.
- Use `port` only for known TCP/UDP port filtering.
- Use `protocol` only with `investigate_protocol`.
- Use `display_filter` only for precise known pivots.
- Keep `max_rows` at `100` unless the request needs more bounded rows.
- Do not request broad full-packet JSON.
- A bounded result with no findings is not proof the PCAP has no useful data; it means this intent and these bounds found none.

## Builder Intent

`_builder_intent` should describe this single bounded call.

Good:

- `Survey visible protocols, hosts, ports, and streams in the supplied PCAP.`
- `Investigate HTTP metadata from the supplied PCAP with bounded rows.`
- `Extract evidence for TCP stream 7 using allowlisted fields.`
- `Search for bounded authentication and credential indicators in the supplied PCAP.`

Bad:

- `Run a full PCAP investigation.`
- `Analyze every packet and protocol.`
- `Dump full packet JSON.`
- `Extract all secrets.`
