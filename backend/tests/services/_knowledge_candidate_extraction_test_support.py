"""Shared test scaffolding for knowledge candidate extraction service tests."""

from __future__ import annotations



from sqlalchemy import create_engine, text

from sqlalchemy.orm import sessionmaker



from agent.providers.llm.core.base import LLMResponse

from backend.database import Base
from backend.models.core import Engagement, Task, User
from backend.models.tenant import Tenant

from backend.services.usage_tracking.models import UsageData



def _build_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = session_factory()
    db.execute(text("PRAGMA foreign_keys=ON"))
    return engine, db

def _seed_user_engagement_task(db):
    tenant = Tenant(id=1, slug="candidate-replay", name="Candidate Replay")
    db.add(tenant)
    db.flush()
    user = User(username="candidate-replay-user", password="secret")
    db.add(user)
    db.flush()
    engagement = Engagement(
        user_id=user.id,
        tenant_id=tenant.id,
        name="Candidate Replay Engagement",
        status="active",
    )
    db.add(engagement)
    db.flush()
    task = Task(
        user_id=user.id,
        tenant_id=tenant.id,
        engagement_id=engagement.id,
        name="Candidate Replay Task",
    )
    db.add(task)
    db.flush()
    return user, engagement, task

class _FakeLLMClient:
    def __init__(self, *, structured_output: dict, usage: UsageData | None = None):
        self._structured_output = structured_output
        self._usage = usage
        self.calls: list[dict[str, str]] = []

    @property
    def model(self) -> str:
        return "gpt-5-mini"

    async def chat_with_usage(self, system_prompt: str, user_prompt: str, **_kwargs) -> LLMResponse:
        self.calls.append({"system_prompt": system_prompt, "user_prompt": user_prompt})
        return LLMResponse(
            content="",
            usage=self._usage,
            structured_output=self._structured_output,
        )

class _RaisingLLMClient:
    @property
    def model(self) -> str:
        return "gpt-5-mini"

    async def chat_with_usage(self, system_prompt: str, user_prompt: str, **_kwargs) -> LLMResponse:
        _ = system_prompt, user_prompt
        raise RuntimeError("provider failure with token=SECRET_TOKEN_123456789")
