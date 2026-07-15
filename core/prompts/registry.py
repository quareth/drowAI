"""Prompt registry for templates and builder singletons.

The registry centralizes prompt template resolution (including `latest.txt`
version manifests) and provides category-specific getters for prompt builders.

Reuse gate contract:
- Prompt consumers must register and resolve templates through this registry and
  `TemplateLoader`; avoid duplicate ad-hoc prompt file resolution in callsites.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from threading import Lock
from typing import Any, Callable, Dict, Optional, Tuple

from .base import ChatPromptBuilder
from .loader import TemplateLoader
from .schemas import BuilderFactory, TemplateId, TemplateRef


@dataclass(slots=True)
class PromptRegistry:
    """Registry for prompt templates and builder instances."""

    templates_root: Path = field(
        default_factory=lambda: Path(__file__).resolve().parent / "versions"
    )

    loader: TemplateLoader = field(init=False)

    _chat_builders: Dict[str, BuilderFactory] = field(default_factory=dict, init=False)
    _post_tool_builders: Dict[str, BuilderFactory] = field(
        default_factory=dict, init=False
    )
    _tool_planning_builders: Dict[str, BuilderFactory] = field(
        default_factory=dict, init=False
    )
    _singletons: Dict[Tuple[str, str], Any] = field(default_factory=dict, init=False)
    _singleton_lock: Lock = field(init=False, repr=False)
    _template_ids: Dict[TemplateId, TemplateRef] = field(
        default_factory=dict, init=False
    )

    def __post_init__(self) -> None:
        self.loader = TemplateLoader(self.templates_root)
        self._singleton_lock = Lock()
        self._register_builtin_templates()
        self._register_builtin_builders()

    def _register_builtin_templates(self) -> None:
        # Stable IDs used by the new prompt management surface.
        self.register_template_id(
            "intent_classifier", family="intent", filename="intent_classifier.txt"
        )
        self.register_template_id(
            "intent_prompt_template", family="intent", filename="prompt_template.txt"
        )
        self.register_template_id(
            "dr_planner_system",
            family="deep_reasoning_planner",
            filename="system.txt",
        )
        self.register_template_id(
            "dr_planner_user",
            family="deep_reasoning_planner",
            filename="user.txt",
        )
        self.register_template_id(
            "simple_chat_system", family="simple_chat", filename="system.txt"
        )
        self.register_template_id(
            "context_compression_system_pass1",
            family="context_compression",
            filename="system_pass1.txt",
        )
        self.register_template_id(
            "context_compression_user_pass1",
            family="context_compression",
            filename="user_pass1.txt",
        )
        self.register_template_id(
            "context_compression_system_pass2",
            family="context_compression",
            filename="system_pass2.txt",
        )
        self.register_template_id(
            "context_compression_user_pass2",
            family="context_compression",
            filename="user_pass2.txt",
        )
        self.register_template_id(
            "tool_output_processing_success",
            family="tool_output_processing",
            filename="success.txt",
        )
        self.register_template_id(
            "tool_output_processing_failure",
            family="tool_output_processing",
            filename="failure.txt",
        )
        self.register_template_id(
            "knowledge_candidate_extraction_system",
            family="knowledge_candidate_extraction",
            filename="system.txt",
        )
        self.register_template_id(
            "knowledge_candidate_extraction_user",
            family="knowledge_candidate_extraction",
            filename="user.txt",
        )
        self.register_template_id(
            "post_tool_system",
            family="post_tool",
            filename="system.txt",
        )
        self.register_template_id(
            "post_tool_articulation_system",
            family="post_tool",
            filename="articulation_system.txt",
        )
        self.register_template_id(
            "post_tool_task_instruction",
            family="post_tool",
            filename="task_instruction.txt",
        )
        self.register_template_id(
            "task_closure_memo_system",
            family="task_closure_memo",
            filename="system.txt",
        )
        self.register_template_id(
            "task_closure_memo_user",
            family="task_closure_memo",
            filename="user.txt",
        )
        self.register_template_id(
            "engagement_report_section_system",
            family="engagement_report_section",
            filename="system.txt",
        )
        self.register_template_id(
            "engagement_report_section_user",
            family="engagement_report_section",
            filename="user.txt",
        )

    def _register_builtin_builders(self) -> None:
        # Keep imports local to avoid import-time cycles for tests that only use TemplateLoader.
        from core.prompts.builders.deep_reasoning import DeepReasoningPromptBuilder
        from core.prompts.builders.post_tool import PostToolReasoningPromptBuilder
        from core.prompts.builders.simple_tool import SimpleToolPromptBuilder
        from core.prompts.builders.tool_planning import ToolPlanningPromptBuilder

        self.register_chat_builder("deep_reasoning", DeepReasoningPromptBuilder)
        self.register_chat_builder("simple_tool", SimpleToolPromptBuilder)
        self.register_post_tool_builder(
            "post_tool_reasoning", PostToolReasoningPromptBuilder
        )
        self.register_tool_planning_builder("tool_planning", ToolPlanningPromptBuilder)

    # ------------------------------------------------------------------
    # Template getters
    # ------------------------------------------------------------------

    def register_template_id(
        self, template_id: TemplateId, family: str, filename: str
    ) -> None:
        """Register a stable template ID to a versioned file location."""

        self._template_ids[template_id] = TemplateRef(family=family, filename=filename)

    def get_latest_version(self, family: str) -> str:
        """Return the version string referenced by `<family>/latest.txt`."""

        return self.loader.get_latest_version(family)

    def get_template(
        self, template_id: TemplateId, version: Optional[str] = None
    ) -> str:
        """Return resolved template contents.

        Resolution:
        - `version is None` or `"latest"` loads from `<family>/latest.txt`
        - explicit version loads `<family>/<version>/<filename>`
        """

        ref = self._template_ids.get(template_id)
        if ref is None:
            raise KeyError(f"Unknown template id: {template_id}")
        if version is None or version == "latest":
            return self.loader.load_latest_version(ref.family, ref.filename)
        return self.loader.load(Path(ref.family) / version / ref.filename)

    # ------------------------------------------------------------------
    # Builder registration
    # ------------------------------------------------------------------

    def register_chat_builder(
        self, name: str, factory: Callable[[], ChatPromptBuilder]
    ) -> None:
        self._chat_builders[name] = factory

    def register_post_tool_builder(self, name: str, factory: BuilderFactory) -> None:
        self._post_tool_builders[name] = factory

    def register_tool_planning_builder(
        self, name: str, factory: BuilderFactory
    ) -> None:
        self._tool_planning_builders[name] = factory

    # ------------------------------------------------------------------
    # Category-specific getters (singleton builders)
    # ------------------------------------------------------------------

    def get_chat_builder(self, name: str) -> ChatPromptBuilder:
        return self._get_singleton("chat", name, self._chat_builders)

    def get_post_tool_builder(self, name: str) -> Any:
        return self._get_singleton("post_tool", name, self._post_tool_builders)

    def get_tool_planning_builder(self, name: str) -> Any:
        return self._get_singleton("tool_planning", name, self._tool_planning_builders)

    def _get_singleton(
        self,
        category: str,
        name: str,
        factories: Dict[str, BuilderFactory],
    ) -> Any:
        key = (category, name)
        if key in self._singletons:
            return self._singletons[key]

        factory = factories.get(name)
        if factory is None:
            raise KeyError(f"No builder registered for {category}:{name}")

        with self._singleton_lock:
            existing = self._singletons.get(key)
            if existing is not None:
                return existing
            instance = factory()
            self._singletons[key] = instance
            return instance


__all__ = ["PromptRegistry"]
