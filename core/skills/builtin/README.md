# Built-in skills

Create one directory per skill and place its complete definition in `SKILL.md`.
Registry initialization discovers every immediate directory and rejects an
invalid package, so no code registration is required.

```yaml
---
name: example_skill
description: Concise model-visible purpose.
metadata:
  version: "1"
  activation: "selectable" # or "mandatory"
  agent-ids: "example_agent"
---

# Guidance

Operational instructions for the compatible subagent.
```

The directory name must match `name`. Use a comma-separated `agent-ids` value
when the same skill is compatible with multiple enabled subagents. Restart or
redeploy normally after adding a package; startup performs discovery and
cross-validation.
