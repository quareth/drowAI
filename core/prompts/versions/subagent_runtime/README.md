# subagent_runtime prompt family

Versioned assets for the generic declarative subagent runtime prompt builder
(`core/prompts/builders/subagent_runtime.py`).

Files:

- `system.txt` - definition-bound role, instructions, runtime loop contract,
  native tool-call guidance, scheduling metadata, and boundary rules.
- `user.txt` - bounded assignment, candidate tool profile, scoped runbooks,
  remaining limits, prior observations, and assignment JSON.

Loaded through `core.prompts.registry.PromptRegistry` using
`subagent_runtime_system` and `subagent_runtime_user`. Bump by creating
`v<N+1>/` and updating `latest.txt`.
