"""Architecture guardrails for tenant_isolation-owned schema columns."""

from __future__ import annotations

from backend.models.chat import AgentLog, ChatMessage, ChatTurnEvent, ToolCall
from backend.models.core import Report, TaskHistory
from backend.models.hitl import InterruptTicket, TurnWorkflow
from backend.models.knowledge import (
    EngagementAssetLink,
    EngagementFindingLink,
    EngagementServiceLink,
    EngagementWebPathLink,
    KnowledgeAsset,
    KnowledgeEntityProvenance,
    KnowledgeFinding,
    KnowledgeRelationship,
    KnowledgeService,
    KnowledgeWebPath,
)
from backend.models.llm import LLMConversation, LLMUsageRecord
from backend.models.streaming import StreamEvent, SystemLog


TENANT_REQUIRED_MODELS = (
    Report,
    TaskHistory,
    AgentLog,
    ChatMessage,
    ChatTurnEvent,
    ToolCall,
    SystemLog,
    StreamEvent,
    TurnWorkflow,
    InterruptTicket,
    LLMConversation,
    LLMUsageRecord,
    KnowledgeAsset,
    KnowledgeService,
    KnowledgeFinding,
    KnowledgeRelationship,
    KnowledgeWebPath,
    EngagementAssetLink,
    EngagementServiceLink,
    EngagementFindingLink,
    EngagementWebPathLink,
    KnowledgeEntityProvenance,
)


def test_tenant_isolation_tenant_owned_models_require_tenant_without_default_tenant_fallback() -> None:
    for model in TENANT_REQUIRED_MODELS:
        column = model.__table__.columns["tenant_id"]
        assert column.nullable is False, model.__name__
        assert column.default is None, model.__name__
        assert column.server_default is None, model.__name__


def test_tenant_isolation_knowledge_canonical_models_use_tenant_scoped_unique_constraints() -> None:
    expected = {
        KnowledgeAsset: "ux_knowledge_assets_tenant_user_asset_key",
        KnowledgeService: "ux_knowledge_services_tenant_user_service_key",
        KnowledgeFinding: "ux_knowledge_findings_tenant_user_finding_key",
        KnowledgeRelationship: "ux_knowledge_relationships_tenant_user_relationship_key",
        KnowledgeWebPath: "ux_knowledge_web_paths_tenant_user_url",
    }

    for model, constraint_name in expected.items():
        constraints = {constraint.name: constraint for constraint in model.__table__.constraints}
        constraint = constraints[constraint_name]
        assert [column.name for column in constraint.columns][0] == "tenant_id"
