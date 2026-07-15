# Runtime Package Manifest

This manifest classifies Python ownership for product runtime packaging.
Packaging and architecture checks read the JSON block below to keep runtime
image, Runner Site package, and management-only code separated.

## Classification

- `runtime_image`: modules allowed inside the packaged Kali runtime image.
- `runner_package`: modules intended for the customer-installed Runner package.
- `management_only`: backend/frontend/control-plane modules that must not be
  imported by runtime-image or Runner package modules.
- `dev_test_only`: tests, mocks, and local developer tooling excluded from
  runtime packaging.

## Runner-Only Product Contract

Product task execution is Management -> Runner -> runtime. The Runner package
may include runner-side code and shared protocol DTOs, but it must not import
backend routers, auth, models, database, runtime-provider services, or frontend
code. Management packaging must not place Docker/runtime ownership in the
control-plane package.

## Machine-Readable Manifest

```json
{
  "runtime_image": {
    "python_roots": [
      "agent/workspace_init.py",
      "kali_executor/__init__.py",
      "kali_executor/communication/__init__.py",
      "kali_executor/communication/file_comm.py",
      "kali_executor/executor_daemon.py",
      "runtime_shared/__init__.py",
      "runtime_shared/file_comm_contracts.py",
      "runtime_shared/runtime_manifest.py",
      "runtime_shared/vpn_observability.py"
    ],
    "required_entrypoint_sources": [
      "agent/workspace_init.py",
      "kali_executor/executor_daemon.py"
    ],
    "excluded_module_prefixes": [],
    "python_module_prefixes": [
      "kali_executor",
      "agent.workspace_init",
      "runtime_shared"
    ]
  },
  "runner_package": {
    "python_roots": [
      "drowai_runner",
      "runtime_shared"
    ],
    "python_module_prefixes": [
      "drowai_runner",
      "runtime_shared"
    ]
  },
  "management_only": {
    "python_roots": [
      "backend/routers",
      "backend/database.py",
      "backend/models",
      "backend/auth.py",
      "backend/services/knowledge",
      "backend/services/artifact",
      "backend/services/terminal",
      "backend/services/llm_provider",
      "backend/services/runtime_provider",
      "backend/services/unified_docker_service.py",
      "client",
      "server",
      "docs"
    ],
    "python_module_prefixes": [
      "backend.routers",
      "backend.database",
      "backend.models",
      "backend.auth",
      "backend.services.knowledge",
      "backend.services.artifact",
      "backend.services.terminal",
      "backend.services.llm_provider",
      "backend.services.runtime_provider",
      "backend.services.unified_docker_service",
      "client",
      "server"
    ]
  },
  "dev_test_only": {
    "python_roots": [
      "tests",
      "backend/tests",
      "agent/tests",
      "kali_executor/tests",
      "scripts",
      "agent/workspaces",
      "agent/durable_knowledge"
    ],
    "python_module_prefixes": [
      "tests",
      "backend.tests",
      "agent.tests",
      "kali_executor.tests",
      "scripts"
    ]
  },
  "transport_classification": {
    "runtime_command_transport": {
      "description": "Prepared command envelopes executable inside the packaged Kali runtime image.",
      "selection": "backend prepares every tool command before dispatch"
    },
    "management_artifact_tool": {
      "description": "Tools that require backend artifact/data-plane services.",
      "tool_id_prefixes": [
        "artifact."
      ]
    },
    "management_knowledge_tool": {
      "description": "Tools that require backend knowledge/indexing services.",
      "tool_ids": [
        "knowledge.cve_lookup"
      ]
    },
    "unsupported_in_runner_v1": {
      "description": "Runner runtime image v1 does not execute management-plane-only tools; lane routing keeps those calls in cloud/data-plane lanes.",
      "derived_from": [
        "management_artifact_tool",
        "management_knowledge_tool"
      ]
    }
  },
  "temporary_exceptions": []
}
```
