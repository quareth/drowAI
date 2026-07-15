"""Import coverage for canonical LangGraph chat subpackage modules."""

from __future__ import annotations


def test_checkpoint_canonical_imports_and_package_loader() -> None:
    """Checkpoint services import through canonical subpackage paths."""
    from backend.services.langgraph_chat import CheckpointerService
    from backend.services.langgraph_chat.checkpoint.anchor_service import (
        CheckpointAnchorService,
    )
    from backend.services.langgraph_chat.checkpoint.checkpointer_service import (
        CheckpointerService as CanonicalCheckpointerService,
    )
    from backend.services.langgraph_chat.checkpoint.continuation_service import (
        CheckpointContinuationService,
    )
    from backend.services.langgraph_chat.checkpoint.execution_config import (
        build_checkpoint_execution_config,
    )

    assert CheckpointerService is CanonicalCheckpointerService
    assert CheckpointAnchorService.__name__ == "CheckpointAnchorService"
    assert CheckpointContinuationService.__name__ == "CheckpointContinuationService"
    assert callable(build_checkpoint_execution_config)


def test_intent_canonical_imports_and_package_loader() -> None:
    """Intent services import through canonical subpackage paths."""
    from backend.services.langgraph_chat import IntentClassifier
    from backend.services.langgraph_chat.intent.briefs import (
        ensure_intent_brief_seed_present,
    )
    from backend.services.langgraph_chat.intent.classifier import (
        IntentClassifier as CanonicalIntentClassifier,
    )
    from backend.services.langgraph_chat.intent.phase_streamer import (
        IntentPhaseStreamer,
    )
    from backend.services.langgraph_chat.intent.prior_turn_references import (
        PriorTurnReferenceMaterializer,
    )
    from backend.services.langgraph_chat.intent.signals import collect_intent_signals

    assert IntentClassifier is CanonicalIntentClassifier
    assert IntentPhaseStreamer.__name__ == "IntentPhaseStreamer"
    assert PriorTurnReferenceMaterializer.__name__ == "PriorTurnReferenceMaterializer"
    assert callable(collect_intent_signals)
    assert callable(ensure_intent_brief_seed_present)


def test_routing_canonical_imports() -> None:
    """Routing helpers import through canonical subpackage paths."""
    from backend.services.langgraph_chat.routing.mode_policy import (
        enforce_plan_mode_availability,
    )
    from backend.services.langgraph_chat.routing.selectors import (
        ChatBranch,
        resolve_branch,
        select_branch,
    )

    assert ChatBranch.NORMAL_CHAT.value == "normal_chat"
    assert callable(enforce_plan_mode_availability)
    assert callable(resolve_branch)
    assert callable(select_branch)
