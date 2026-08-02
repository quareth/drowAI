# Browser Testing Runbook — HTB Cap Credential Collection

Manual QA runbook for testing DrowAI tool execution against the assigned Hack
The Box **Cap** machine.

The goal is to verify that the agent can execute the required tools, carry
their output into the next prompt, download a PCAP, and collect any username
and password present in that PCAP. This scenario does not test exploitation,
flag retrieval, or privilege escalation.

---

## Pre-flight

1. Open **Outpost → Operations**.
2. Select the existing task identified by `<TASK_NAME>`.
3. Confirm the task and container are **Running**.
4. Confirm the HTB VPN is connected.
5. Set the model to `<MODEL_NAME>` with reasoning effort
   `<REASONING_EFFORT>`.
6. Set the agent mode to **Agent (Full Access)**.
7. Set the **Plan** toggle to **off**.
8. Confirm all three selections before continuing.
9. Confirm the current Cap target IP as `<TARGET_IP>`.

### Run variables

Choose these values before starting and replace every matching placeholder in
the prompts. Do not paste unresolved placeholders into the conversation.

| Placeholder | Value for this run |
| --- | --- |
| `<TASK_NAME>` | Existing running DrowAI task used for the test |
| `<MODEL_NAME>` | Model being validated |
| `<REASONING_EFFORT>` | Reasoning effort supported by the selected model |
| `<TARGET_IP>` | Current IP assigned to the HTB Cap instance |
| `<CAPTURE_ID>` | Historical capture record selected after Prompt 4 |
| `<PCAP_PATH>` | Task-local output path, such as `artifacts/htb-cap/capture.pcap` |

Keep the task, model, reasoning effort, target IP, and PCAP path unchanged for
the remainder of one test run. Set `<CAPTURE_ID>` after reviewing the FFUF
results from Prompt 4.

### Waiting rule

Send one prompt at a time.

- Wait for the current tool and the agent response to finish before sending the
  next prompt.
- Do not stop a tool because it is temporarily quiet.
- Give slow tools at least **2–3 minutes**.
- A tool may take up to **5 minutes** normally.
- The execution timeout is **10 minutes**. Do not interrupt before the timeout
  unless the tool reports a terminal failure.

---

## Scenario — Discover and analyze the exposed PCAP

### Goal

Run the exact tool sequence needed to reach and analyze the Cap machine's
downloadable packet capture.

### Setup

1. Confirm the selected task is `<TASK_NAME>`.
2. Confirm the model is `<MODEL_NAME>` with reasoning effort
   `<REASONING_EFFORT>`.
3. Confirm the agent mode is **Agent (Full Access)**.
4. Confirm the **Plan** toggle is **off**.
5. Start with an empty conversation when possible.

---

## Prompt 1 — Nmap

Paste verbatim:

```text
Run the Nmap tool against <TARGET_IP> for TCP ports 21, 22, and 80. Skip host discovery, enable service-version detection, and report the state and detected service for each port. Use the Nmap tool, not a generic shell command.
```

Wait for the Nmap tool and agent response to finish.

---

## Prompt 2 — Inspect the web service

Paste verbatim:

```text
Use the HTTP request tool to send a GET request to http://<TARGET_IP>/. Return the HTTP status, page title, visible account name, and every application path or navigation link found in the response body. Use the HTTP request tool, not a generic shell command.
```

Wait for the HTTP request and agent response to finish.

---

## Prompt 3 — Inspect the capture action

Paste verbatim:

```text
Use the HTTP request tool to send a GET request to http://<TARGET_IP>/capture with redirect following disabled. Return the HTTP status and the exact Location header. Use the HTTP request tool, not a generic shell command.
```

Wait for the HTTP request and agent response to finish.

---

## Prompt 4 — Enumerate capture records with FFUF

Paste verbatim:

```text
Use the FFUF crawler tool against http://<TARGET_IP>/data/FUZZ. Use an inline wordlist containing 0, 1, 2, 3, 4, 5, 6, 7, 8, 9, and 10. Match HTTP statuses 200 through 599. Do not follow redirects and do not recurse. Report every result with its URL, status, response size, and redirect location. Use the FFUF crawler tool, not a generic shell command.
```

Wait for FFUF and the agent response to finish. FFUF may take several minutes.

---

## Prompt 5 — Inspect the historical capture page

Paste verbatim:

```text
Use the HTTP request tool to send a GET request to http://<TARGET_IP>/data/<CAPTURE_ID>. Inspect the response body and report the exact URL of every downloadable file or download action shown on the page. Use the HTTP request tool, not a generic shell command.
```

Wait for the HTTP request and agent response to finish.

---

## Prompt 6 — Download the PCAP

Paste verbatim:

```text
Use the HTTP download tool to download http://<TARGET_IP>/download/<CAPTURE_ID> into the task workspace at <PCAP_PATH>. Allow overwriting that exact test artifact if it already exists. Report the saved path, transfer result, byte count, content type or detected file type, and checksum. Use the HTTP download tool, not a generic shell command.
```

Wait until the download tool and agent response are fully complete.

---

## Prompt 7 — Survey the PCAP

Paste verbatim:

```text
Use the TShark tool to analyze <PCAP_PATH>. Set analysis_mode to survey, max_rows to 100, include_payload_indicators to false, and sensitive_proof_mode to proof_excerpt. Report the protocols, services, hosts, ports, and interesting streams found in the PCAP. Use the TShark tool, not a generic shell command.
```

Wait for TShark and the agent response to finish.

---

## Prompt 8 — Extract credentials from the PCAP

Paste verbatim:

```text
Use the TShark tool again on <PCAP_PATH>. Set analysis_mode to find_security_relevant_artifacts, max_rows to 100, and sensitive_proof_mode to proof_excerpt. Report every username and password found in the packet capture, the protocol and stream they belong to, the supporting frame numbers, and the server authentication result. Do not use any recovered credential. Use the TShark tool, not a generic shell command.
```

Wait for TShark and the agent response to finish. Do not interrupt while the
tool or observation card is still active.

---

## Prompt 9 — Final result

Paste verbatim:

```text
Using only the tool output from this conversation, report the open ports and services, discovered web paths, capture record and download URL, saved PCAP path, protocols found in the PCAP, and every recovered username and password with its packet evidence. Do not run another tool and do not authenticate or exploit the target.
```

The scenario ends after the agent returns this response.

---

## Run notes

Record only execution behavior:

```text
Date/time:
Tester:
Task:
Model/provider:
Reasoning effort:
Target IP:
Capture ID:
PCAP path:

Prompt 1 completed:
Prompt 2 completed:
Prompt 3 completed:
Prompt 4 completed:
Prompt 5 completed:
Prompt 6 completed:
Prompt 7 completed:
Prompt 8 completed:
Prompt 9 completed:

Any failed tool:
Any interrupted tool:
Any tool reaching 5 minutes:
Any tool reaching the 10-minute timeout:
Any generic shell substitution:
Stopping boundary respected:
Notes:
```

Do not commit recovered plaintext credentials or a transcript containing them.
