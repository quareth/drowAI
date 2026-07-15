# finalizer prompt family — v1

Versioned assets for the unified finalizer (`core/prompts/builders/finalize_results.py`).

Files:

- `system_base.txt` — operator voice and four-part output skeleton (`## Action`, `## Findings`, `## Impact`, `## Recommended Next Action`). Loaded for every finalizer call.
- `addendum_retry.txt` — appended to the system prompt when more than one tool attempt happened for the turn.
- `addendum_dr.txt` — appended to the system prompt when capability is `deep_reasoning`.
- `addendum_analyst.txt` — appended to the system prompt when analyst-derived (PTR candidate) observations are available.
- `instructions.txt` — user-prompt closer that re-states the four-part contract and grounding rules.

Loaded via `core.prompts.loader.TemplateLoader.load_latest_version("finalizer", <filename>)`. Bumping versions follows the standard convention: create `v<N+1>/` next to `v1/` and update `latest.txt`.
