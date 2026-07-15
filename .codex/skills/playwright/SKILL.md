---
name: playwright
description: "Use when the task requires automating a real browser from the terminal through Playwright CLI: navigation, form filling, snapshots, screenshots, data extraction, UI-flow debugging, or local web app inspection via the bundled `.codex/skills/playwright/scripts/playwright_cli.sh` wrapper."
---

# Playwright CLI Skill

Drive a real browser from the terminal using `playwright-cli`. Prefer the bundled wrapper script so the CLI works even when it is not globally installed.
Treat this skill as CLI-first automation. Do not pivot to `@playwright/test` unless the user explicitly asks for test files.

## Prerequisite Check

Before proposing commands, check whether `npx` is available because the wrapper depends on it:

```bash
command -v npx >/dev/null 2>&1
```

If it is not available, pause and ask the user to install Node.js/npm, which provides `npx`. Provide these steps verbatim:

```bash
# Verify Node/npm are installed
node --version
npm --version

# If missing, install Node.js/npm, then:
npm install -g @playwright/cli@latest
playwright-cli --help
```

Once `npx` is present, proceed with the wrapper script. A global install of `playwright-cli` is optional.

## Skill Path

From the project root, point to this skill's wrapper:

```bash
export PWCLI=".codex/skills/playwright/scripts/playwright_cli.sh"
```

If the skill is installed personally under `~/.codex/skills/playwright/`:

```bash
export PWCLI="$HOME/.codex/skills/playwright/scripts/playwright_cli.sh"
```

## Windows

On Windows, the wrapper `playwright_cli.sh` is a Bash script. Use either PowerShell with `npx` or run the script from Git Bash if available.

PowerShell prerequisite check:

```powershell
npx --version
```

If `npx` is missing, install Node.js, then run the check again.

Option A - use `npx` directly:

```powershell
function PWCLI { npx --yes --package @playwright/cli playwright-cli @args }
```

Then run:

```powershell
PWCLI open https://playwright.dev --headed
PWCLI snapshot
PWCLI click e15
PWCLI type "Playwright"
PWCLI press Enter
PWCLI screenshot
```

Option B - wrapper via Git Bash:

```bash
export PWCLI=".codex/skills/playwright/scripts/playwright_cli.sh"
"$PWCLI" open https://playwright.dev --headed
"$PWCLI" snapshot
```

Use forward slashes in paths when setting them in Bash on Windows. First run will trigger `npx` to fetch `@playwright/cli`; install browsers once with `npx playwright install` if prompted.

## Quick Start

Use the wrapper script:

```bash
"$PWCLI" open https://playwright.dev --headed
"$PWCLI" snapshot
"$PWCLI" click e15
"$PWCLI" type "Playwright"
"$PWCLI" press Enter
"$PWCLI" screenshot
```

If the user prefers a global install, this is also valid:

```bash
npm install -g @playwright/cli@latest
playwright-cli --help
```

## Core Workflow

1. Open the page.
2. Snapshot to get stable element refs.
3. Interact using refs from the latest snapshot.
4. Re-snapshot after navigation or significant DOM changes.
5. Capture artifacts such as screenshots, PDFs, or traces when useful.

Minimal loop:

```bash
"$PWCLI" open https://example.com
"$PWCLI" snapshot
"$PWCLI" click e3
"$PWCLI" snapshot
```

## When To Snapshot Again

Snapshot again after:
- navigation
- clicking elements that change the UI substantially
- opening/closing modals or menus
- tab switches

Refs can go stale. When a command fails due to a missing ref, snapshot again.

## Recommended Patterns

### Form Fill And Submit

```bash
"$PWCLI" open https://example.com/form
"$PWCLI" snapshot
"$PWCLI" fill e1 "user@example.com"
"$PWCLI" fill e2 "password123"
"$PWCLI" click e3
"$PWCLI" snapshot
```

### Debug A UI Flow With Traces

```bash
"$PWCLI" open https://example.com --headed
"$PWCLI" tracing-start
# interactions...
"$PWCLI" tracing-stop
```

### Multi-Tab Work

```bash
"$PWCLI" tab-new https://example.com
"$PWCLI" tab-list
"$PWCLI" tab-select 0
"$PWCLI" snapshot
```

## Wrapper Script

The wrapper script uses `npx --package @playwright/cli playwright-cli` so the CLI can run without a global install:

```bash
"$PWCLI" --help
```

Prefer the wrapper unless the repository already standardizes on a global install.

## References

Open only what you need:
- CLI command reference: `references/cli.md`
- Practical workflows and troubleshooting: `references/workflows.md`

## Guardrails

- Always snapshot before referencing element ids like `e12`.
- Re-snapshot when refs seem stale.
- Prefer explicit commands over `eval` and `run-code` unless needed.
- When you do not have a fresh snapshot, use placeholder refs like `eX` and say why; do not bypass refs with `run-code`.
- Use `--headed` when a visual check will help.
- When capturing artifacts in this repo, use `output/playwright/` and avoid introducing new top-level artifact folders.
- Default to CLI commands and workflows, not Playwright test specs.
