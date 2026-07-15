"""Initial current schema baseline.

Revision ID: 0001_initial_current_schema
Revises: 
Create Date: 2026-07-08 12:54:10.946486+00:00

"""
from typing import Sequence, Union

from alembic import op
import pgvector.sqlalchemy.vector
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = '0001_initial_current_schema'
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _guid_type() -> sa.types.TypeEngine:
    """Return the baseline UUID type without importing application modules."""

    return sa.CHAR(36).with_variant(postgresql.UUID(as_uuid=True), "postgresql")


TENANT_POLICY_TABLES: tuple[str, ...] = (
    "agent_logs",
    "artifact_manifests",
    "chat_messages",
    "chat_turn_events",
    "engagement_asset_links",
    "engagement_finding_links",
    "engagement_report_jobs",
    "engagement_reports",
    "engagement_service_links",
    "engagement_web_path_links",
    "engagements",
    "execution_artifacts",
    "execution_sites",
    "interrupt_tickets",
    "knowledge_assets",
    "knowledge_entity_provenance",
    "knowledge_evidence_archives",
    "knowledge_findings",
    "knowledge_ingestion_runs",
    "knowledge_observations",
    "knowledge_relationships",
    "knowledge_services",
    "knowledge_web_paths",
    "llm_conversations",
    "llm_usage_records",
    "reports",
    "runner_connections",
    "runner_control_messages",
    "runner_credentials",
    "runner_install_tokens",
    "runners",
    "runtime_jobs",
    "stream_events",
    "system_logs",
    "task_closure_memos",
    "task_history",
    "tasks",
    "tenant_data_management_settings",
    "tool_calls",
    "tool_executions",
    "turn_workflows",
)

FEATURE_GATE_ENABLED_EXPR = (
    "COALESCE(NULLIF(current_setting('app.rls_enabled', true), ''), 'off') "
    "IN ('1', 'true', 'yes', 'on')"
)
PRIVILEGED_BYPASS_EXPR = (
    "COALESCE(NULLIF(current_setting('app.rls_bypass', true), ''), 'off') "
    "IN ('1', 'true', 'yes', 'on')"
)
CURRENT_TENANT_ID_EXPR = "NULLIF(current_setting('app.current_tenant_id', true), '')::integer"
CURRENT_USER_ID_EXPR = "NULLIF(current_setting('app.current_user_id', true), '')::integer"
CURRENT_ACTOR_TYPE_EXPR = "NULLIF(current_setting('app.current_actor_type', true), '')"
RLS_BYPASS_OR_DISABLED_EXPR = f"({PRIVILEGED_BYPASS_EXPR} OR NOT {FEATURE_GATE_ENABLED_EXPR})"
OWNER_ADMIN_ACTOR_EXPR = (
    f"LOWER(COALESCE({CURRENT_ACTOR_TYPE_EXPR}, '')) "
    "IN ('tenant_owner', 'tenant_admin')"
)


def _is_postgresql() -> bool:
    return str(getattr(op.get_bind().dialect, "name", "")).lower() == "postgresql"


def _seed_default_tenant() -> None:
    if _is_postgresql():
        op.execute(
            sa.text(
                """
                INSERT INTO tenants (id, slug, name)
                VALUES (1, 'default', 'Default Tenant')
                ON CONFLICT (id) DO NOTHING
                """
            )
        )
        op.execute(
            sa.text(
                """
                SELECT setval(
                    pg_get_serial_sequence('tenants', 'id'),
                    GREATEST((SELECT COALESCE(MAX(id), 1) FROM tenants), 1),
                    true
                )
                """
            )
        )
        return

    op.execute(
        sa.text(
            """
            INSERT OR IGNORE INTO tenants (id, slug, name)
            VALUES (1, 'default', 'Default Tenant')
            """
        )
    )


def _create_policy(
    policy_name: str,
    table_name: str,
    *,
    command: str,
    using_expr: str | None = None,
    check_expr: str | None = None,
) -> None:
    parts = [f"CREATE POLICY {policy_name} ON {table_name} FOR {command}"]
    if using_expr is not None:
        parts.append(f"USING ({using_expr})")
    if check_expr is not None:
        parts.append(f"WITH CHECK ({check_expr})")
    op.execute(sa.text(" ".join(parts)))


def _enable_forced_rls(table_name: str) -> None:
    op.execute(sa.text(f"ALTER TABLE {table_name} ENABLE ROW LEVEL SECURITY"))
    op.execute(sa.text(f"ALTER TABLE {table_name} FORCE ROW LEVEL SECURITY"))


def _create_generic_tenant_policy(table_name: str) -> None:
    scope_expr = f"{RLS_BYPASS_OR_DISABLED_EXPR} OR tenant_id = {CURRENT_TENANT_ID_EXPR}"
    _create_policy(
        f"tenant_isolation_{table_name}_scope",
        table_name,
        command="ALL",
        using_expr=scope_expr,
        check_expr=scope_expr,
    )


def _create_tenant_policies() -> None:
    _enable_forced_rls("tenants")

    membership_exists_expr = (
        "EXISTS ("
        "SELECT 1 FROM tenant_memberships AS tm "
        "WHERE tm.tenant_id = tenants.id "
        f"AND tm.user_id = {CURRENT_USER_ID_EXPR} "
        "AND LOWER(COALESCE(tm.status, 'active')) = 'active' "
        "AND tm.deactivated_at IS NULL"
        ")"
    )
    owner_admin_membership_exists_expr = (
        "EXISTS ("
        "SELECT 1 FROM tenant_memberships AS tm "
        "WHERE tm.tenant_id = tenants.id "
        f"AND tm.user_id = {CURRENT_USER_ID_EXPR} "
        "AND LOWER(COALESCE(tm.status, 'active')) = 'active' "
        "AND tm.deactivated_at IS NULL "
        "AND LOWER(COALESCE(tm.role, '')) IN ('owner', 'admin')"
        ")"
    )

    read_expr = (
        f"{RLS_BYPASS_OR_DISABLED_EXPR} "
        f"OR id = {CURRENT_TENANT_ID_EXPR} "
        "OR ("
        "LOWER(COALESCE(tenants.status, 'active')) = 'active' "
        "AND tenants.deactivated_at IS NULL "
        f"AND {membership_exists_expr}"
        ")"
    )
    write_expr = (
        f"{RLS_BYPASS_OR_DISABLED_EXPR} "
        "OR ("
        f"id = {CURRENT_TENANT_ID_EXPR} "
        f"AND {owner_admin_membership_exists_expr}"
        ")"
    )
    insert_expr = f"{RLS_BYPASS_OR_DISABLED_EXPR} OR id = {CURRENT_TENANT_ID_EXPR}"

    _create_policy(
        "tenant_isolation_tenants_membership_read",
        "tenants",
        command="SELECT",
        using_expr=read_expr,
    )
    _create_policy(
        "tenant_isolation_tenants_owner_admin_update",
        "tenants",
        command="UPDATE",
        using_expr=write_expr,
        check_expr=write_expr,
    )
    _create_policy(
        "tenant_isolation_tenants_owner_admin_delete",
        "tenants",
        command="DELETE",
        using_expr=write_expr,
    )
    _create_policy(
        "tenant_isolation_tenants_owner_admin_insert",
        "tenants",
        command="INSERT",
        check_expr=insert_expr,
    )


def _create_tenant_membership_policies() -> None:
    _enable_forced_rls("tenant_memberships")

    read_expr = (
        f"{RLS_BYPASS_OR_DISABLED_EXPR} "
        "OR ("
        f"user_id = {CURRENT_USER_ID_EXPR} "
        "AND LOWER(COALESCE(status, 'active')) = 'active' "
        "AND deactivated_at IS NULL "
        f"AND ({CURRENT_TENANT_ID_EXPR} IS NULL OR tenant_id = {CURRENT_TENANT_ID_EXPR})"
        ") "
        "OR ("
        f"tenant_id = {CURRENT_TENANT_ID_EXPR} "
        f"AND {CURRENT_USER_ID_EXPR} IS NOT NULL "
        f"AND {CURRENT_TENANT_ID_EXPR} IS NOT NULL"
        ")"
    )
    write_expr = (
        f"{RLS_BYPASS_OR_DISABLED_EXPR} "
        "OR ("
        f"tenant_id = {CURRENT_TENANT_ID_EXPR} "
        f"AND {CURRENT_USER_ID_EXPR} IS NOT NULL "
        f"AND {CURRENT_TENANT_ID_EXPR} IS NOT NULL "
        f"AND {OWNER_ADMIN_ACTOR_EXPR}"
        ")"
    )

    _create_policy(
        "tenant_isolation_tenant_memberships_user_lookup_read",
        "tenant_memberships",
        command="SELECT",
        using_expr=read_expr,
    )
    _create_policy(
        "tenant_isolation_tenant_memberships_owner_admin_update",
        "tenant_memberships",
        command="UPDATE",
        using_expr=write_expr,
        check_expr=write_expr,
    )
    _create_policy(
        "tenant_isolation_tenant_memberships_owner_admin_delete",
        "tenant_memberships",
        command="DELETE",
        using_expr=write_expr,
    )
    _create_policy(
        "tenant_isolation_tenant_memberships_owner_admin_insert",
        "tenant_memberships",
        command="INSERT",
        check_expr=write_expr,
    )


def _create_semantic_memory_policy() -> None:
    _enable_forced_rls("semantic_memories")

    scope_expr = (
        f"{RLS_BYPASS_OR_DISABLED_EXPR} "
        "OR ("
        f"tenant_id = {CURRENT_TENANT_ID_EXPR} "
        "OR ("
        "tenant_id IS NULL "
        "AND memory_tier = 'user_profile' "
        f"AND user_id = {CURRENT_USER_ID_EXPR}"
        ")"
        ")"
    )
    _create_policy(
        "tenant_isolation_semantic_memories_scope",
        "semantic_memories",
        command="ALL",
        using_expr=scope_expr,
        check_expr=scope_expr,
    )


def _create_tenant_isolation_rls() -> None:
    if not _is_postgresql():
        return

    _create_tenant_policies()
    _create_tenant_membership_policies()
    for table_name in TENANT_POLICY_TABLES:
        _enable_forced_rls(table_name)
        _create_generic_tenant_policy(table_name)
    _create_semantic_memory_policy()


def upgrade() -> None:
    if _is_postgresql():
        op.execute(sa.text("CREATE EXTENSION IF NOT EXISTS vector"))

    op.create_table('cve_index_settings',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('enabled', sa.Boolean(), server_default='false', nullable=False),
    sa.Column('daily_sync_hour_utc', sa.Integer(), server_default='2', nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_cve_index_settings_id'), 'cve_index_settings', ['id'], unique=False)
    op.create_table('cve_index_sync_runs',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('trigger_kind', sa.String(length=24), server_default='manual', nullable=False),
    sa.Column('sync_kind', sa.String(length=24), nullable=False),
    sa.Column('status', sa.String(length=24), server_default='running', nullable=False),
    sa.Column('baseline_date', sa.Date(), nullable=True),
    sa.Column('delta_from_hour_utc', sa.DateTime(timezone=True), nullable=True),
    sa.Column('delta_to_hour_utc', sa.DateTime(timezone=True), nullable=True),
    sa.Column('phase', sa.String(length=24), nullable=True),
    sa.Column('progress_updated_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('started_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('processed_records', sa.Integer(), server_default='0', nullable=False),
    sa.Column('inserted_records', sa.Integer(), server_default='0', nullable=False),
    sa.Column('updated_records', sa.Integer(), server_default='0', nullable=False),
    sa.Column('error_message', sa.Text(), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_cve_index_sync_runs_finished_at', 'cve_index_sync_runs', ['finished_at'], unique=False)
    op.create_index(op.f('ix_cve_index_sync_runs_id'), 'cve_index_sync_runs', ['id'], unique=False)
    op.create_index('ix_cve_index_sync_runs_started_at', 'cve_index_sync_runs', ['started_at'], unique=False)
    op.create_index('ix_cve_index_sync_runs_status', 'cve_index_sync_runs', ['status'], unique=False)
    op.create_table('cve_records',
    sa.Column('id', sa.BigInteger(), nullable=False),
    sa.Column('cve_id', sa.String(length=32), nullable=False),
    sa.Column('source', sa.String(length=24), server_default='cvelist_v5', nullable=False),
    sa.Column('record_state', sa.String(length=24), server_default='published', nullable=False),
    sa.Column('published_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('source_updated_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('title', sa.Text(), nullable=True),
    sa.Column('description', sa.Text(), nullable=True),
    sa.Column('severity', sa.String(length=24), nullable=True),
    sa.Column('metrics', sa.JSON(), nullable=True),
    sa.Column('weaknesses', sa.JSON(), nullable=True),
    sa.Column('references', sa.JSON(), nullable=True),
    sa.Column('cve_json', sa.JSON(), nullable=False),
    sa.Column('projection_status', sa.String(length=32), server_default='pending', nullable=False),
    sa.Column('projection_affected_count', sa.Integer(), server_default=sa.text('0'), nullable=False),
    sa.Column('projection_error_code', sa.String(length=64), nullable=True),
    sa.Column('projection_last_projected_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('cve_id')
    )
    op.create_index('ix_cve_records_cve_id', 'cve_records', ['cve_id'], unique=False)
    op.create_index(op.f('ix_cve_records_id'), 'cve_records', ['id'], unique=False)
    op.create_index('ix_cve_records_projection_status', 'cve_records', ['projection_status'], unique=False)
    op.create_index('ix_cve_records_record_state', 'cve_records', ['record_state'], unique=False)
    op.create_index('ix_cve_records_source_updated_at', 'cve_records', ['source_updated_at'], unique=False)
    op.create_table('platform_installations',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('deployment_profile', sa.String(length=32), nullable=False),
    sa.Column('network_config', sa.JSON(), nullable=True),
    sa.Column('display_defaults', sa.JSON(), nullable=True),
    sa.Column('setup_version', sa.Integer(), server_default='1', nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('task_turn_counter',
    sa.Column('task_id', sa.Integer(), nullable=False),
    sa.Column('next_turn', sa.Integer(), server_default='1', nullable=False),
    sa.PrimaryKeyConstraint('task_id')
    )
    op.create_table('tenants',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('slug', sa.String(length=64), nullable=False),
    sa.Column('name', sa.String(length=255), nullable=False),
    sa.Column('status', sa.String(length=32), server_default='active', nullable=False),
    sa.Column('max_concurrent_tasks', sa.Integer(), nullable=True),
    sa.Column('max_concurrent_tasks_per_user', sa.Integer(), nullable=True),
    sa.Column('deactivated_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_tenants_id'), 'tenants', ['id'], unique=False)
    op.create_index(op.f('ix_tenants_slug'), 'tenants', ['slug'], unique=True)
    _seed_default_tenant()
    op.create_table('users',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('username', sa.String(length=255), nullable=False),
    sa.Column('password', sa.String(length=255), nullable=False),
    sa.Column('email', sa.String(length=255), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
    sa.Column('is_active', sa.Boolean(), nullable=True),
    sa.Column('max_concurrent_tasks', sa.Integer(), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_users_email'), 'users', ['email'], unique=True)
    op.create_index(op.f('ix_users_id'), 'users', ['id'], unique=False)
    op.create_index(op.f('ix_users_username'), 'users', ['username'], unique=True)
    op.create_table('cve_affected_products',
    sa.Column('id', sa.BigInteger(), nullable=False),
    sa.Column('cve_record_id', sa.BigInteger(), nullable=False),
    sa.Column('cve_id', sa.String(length=32), nullable=False),
    sa.Column('vendor_raw', sa.Text(), nullable=True),
    sa.Column('vendor_norm', sa.String(length=255), nullable=True),
    sa.Column('product_raw', sa.Text(), nullable=True),
    sa.Column('product_norm', sa.String(length=255), nullable=True),
    sa.Column('default_status', sa.String(length=32), nullable=True),
    sa.Column('versions_json', sa.JSON(), nullable=True),
    sa.Column('cpes_json', sa.JSON(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.ForeignKeyConstraint(['cve_record_id'], ['cve_records.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_cve_affected_products_cve_id', 'cve_affected_products', ['cve_id'], unique=False)
    op.create_index('ix_cve_affected_products_cve_record_id', 'cve_affected_products', ['cve_record_id'], unique=False)
    op.create_index(op.f('ix_cve_affected_products_id'), 'cve_affected_products', ['id'], unique=False)
    op.create_index('ix_cve_affected_products_product_norm', 'cve_affected_products', ['product_norm'], unique=False)
    op.create_index('ix_cve_affected_products_vendor_norm', 'cve_affected_products', ['vendor_norm'], unique=False)
    op.create_index('ix_cve_affected_products_vendor_product_norm', 'cve_affected_products', ['vendor_norm', 'product_norm'], unique=False)
    op.create_table('cve_index_state',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('last_sync_status', sa.String(length=24), server_default='idle', nullable=False),
    sa.Column('last_successful_sync_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_attempt_started_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_attempt_finished_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_error', sa.Text(), nullable=True),
    sa.Column('last_applied_baseline_date', sa.Date(), nullable=True),
    sa.Column('last_applied_delta_hour_utc', sa.DateTime(timezone=True), nullable=True),
    sa.Column('rebuild_required', sa.Boolean(), server_default='false', nullable=False),
    sa.Column('active_run_id', sa.Integer(), nullable=True),
    sa.Column('lease_owner_id', sa.String(length=128), nullable=True),
    sa.Column('lease_heartbeat_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('lease_expires_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('current_phase', sa.String(length=24), nullable=True),
    sa.Column('progress_updated_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.ForeignKeyConstraint(['active_run_id'], ['cve_index_sync_runs.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_cve_index_state_id'), 'cve_index_state', ['id'], unique=False)
    op.create_index('ix_cve_index_state_last_attempt_finished_at', 'cve_index_state', ['last_attempt_finished_at'], unique=False)
    op.create_index('ix_cve_index_state_last_attempt_started_at', 'cve_index_state', ['last_attempt_started_at'], unique=False)
    op.create_index('ix_cve_index_state_last_sync_status', 'cve_index_state', ['last_sync_status'], unique=False)
    op.create_index('ix_cve_index_state_lease_expires_at', 'cve_index_state', ['lease_expires_at'], unique=False)
    op.create_table('engagement_reports',
    sa.Column('id', _guid_type(), nullable=False),
    sa.Column('schema_version', sa.String(length=64), server_default='1', nullable=False),
    sa.Column('tenant_id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('created_by_user_id', sa.Integer(), nullable=False),
    sa.Column('engagement_id', sa.Integer(), nullable=False),
    sa.Column('engagement_name_snapshot', sa.String(length=255), nullable=True),
    sa.Column('engagement_status_snapshot', sa.String(length=32), nullable=True),
    sa.Column('report_type', sa.String(length=64), nullable=False),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.Column('status', sa.String(length=32), nullable=False),
    sa.Column('is_current', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('title', sa.String(length=255), nullable=False),
    sa.Column('sections', sa.JSON(), nullable=False),
    sa.Column('markdown_snapshot', sa.Text(), nullable=True),
    sa.Column('source_task_memo_ids', sa.JSON(), nullable=False),
    sa.Column('source_knowledge_refs', sa.JSON(), nullable=False),
    sa.Column('source_evidence_refs', sa.JSON(), nullable=False),
    sa.Column('generation_metadata', sa.JSON(), nullable=True),
    sa.Column('error_message', sa.Text(), nullable=True),
    sa.Column('delete_scheduled_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('delete_undo_until', sa.DateTime(timezone=True), nullable=True),
    sa.Column('deletion_finalized_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('deleted_by_user_id', sa.Integer(), nullable=True),
    sa.Column('deletion_reason', sa.String(length=64), nullable=True),
    sa.Column('deletion_metadata', sa.JSON(), nullable=True),
    sa.Column('deletion_original_is_current', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column('generated_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['created_by_user_id'], ['users.id'], ),
    sa.ForeignKeyConstraint(['deleted_by_user_id'], ['users.id'], ),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_engagement_reports_tenant_delete_undo', 'engagement_reports', ['tenant_id', 'delete_undo_until'], unique=False)
    op.create_index('ix_engagement_reports_tenant_deletion_finalized', 'engagement_reports', ['tenant_id', 'deletion_finalized_at'], unique=False)
    op.create_index('ix_engagement_reports_tenant_engagement_created', 'engagement_reports', ['tenant_id', 'engagement_id', 'created_at'], unique=False)
    op.create_index(op.f('ix_engagement_reports_tenant_id'), 'engagement_reports', ['tenant_id'], unique=False)
    op.create_index('ix_engagement_reports_tenant_status', 'engagement_reports', ['tenant_id', 'status'], unique=False)
    op.create_index('ix_engagement_reports_tenant_user_created', 'engagement_reports', ['tenant_id', 'user_id', 'created_at'], unique=False)
    op.create_index('ix_engagement_reports_tenant_user_engagement_type_current', 'engagement_reports', ['tenant_id', 'user_id', 'engagement_id', 'report_type', 'is_current'], unique=False)
    op.create_index(op.f('ix_engagement_reports_user_id'), 'engagement_reports', ['user_id'], unique=False)
    op.create_index('ux_engagement_reports_current_ready', 'engagement_reports', ['tenant_id', 'user_id', 'engagement_id', 'report_type'], unique=True, sqlite_where=sa.text("status = 'ready' AND is_current IS true"), postgresql_where=sa.text("status = 'ready' AND is_current IS true"))
    op.create_index('ux_engagement_reports_version', 'engagement_reports', ['tenant_id', 'user_id', 'engagement_id', 'report_type', 'version'], unique=True)
    op.create_table('engagements',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('tenant_id', sa.Integer(), server_default='1', nullable=False),
    sa.Column('name', sa.String(length=255), nullable=False),
    sa.Column('description', sa.Text(), nullable=True),
    sa.Column('status', sa.String(length=32), nullable=False),
    sa.Column('metadata', sa.JSON(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_engagements_id'), 'engagements', ['id'], unique=False)
    op.create_index(op.f('ix_engagements_tenant_id'), 'engagements', ['tenant_id'], unique=False)
    op.create_index(op.f('ix_engagements_user_id'), 'engagements', ['user_id'], unique=False)
    op.create_table('execution_sites',
    sa.Column('id', _guid_type(), nullable=False),
    sa.Column('tenant_id', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(length=255), nullable=False),
    sa.Column('slug', sa.String(length=128), nullable=False),
    sa.Column('network_label', sa.String(length=255), nullable=True),
    sa.Column('status', sa.String(length=32), server_default='active', nullable=False),
    sa.Column('labels_json', sa.JSON(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('tenant_id', 'name', name='uq_execution_sites_tenant_name'),
    sa.UniqueConstraint('tenant_id', 'slug', name='uq_execution_sites_tenant_slug')
    )
    op.create_index(op.f('ix_execution_sites_tenant_id'), 'execution_sites', ['tenant_id'], unique=False)
    op.create_index('ix_execution_sites_tenant_status', 'execution_sites', ['tenant_id', 'status'], unique=False)
    op.create_table('knowledge_entity_provenance',
    sa.Column('id', _guid_type(), nullable=False),
    sa.Column('tenant_id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('entity_type', sa.String(length=32), nullable=False),
    sa.Column('entity_id', _guid_type(), nullable=False),
    sa.Column('engagement_id', sa.Integer(), nullable=True),
    sa.Column('task_id', sa.Integer(), nullable=True),
    sa.Column('execution_id', _guid_type(), nullable=True),
    sa.Column('tool_name', sa.String(length=255), nullable=True),
    sa.Column('ingestion_run_id', _guid_type(), nullable=True),
    sa.Column('observed_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('confidence', sa.String(length=32), nullable=True),
    sa.Column('evidence_archive_id', _guid_type(), nullable=True),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_knowledge_entity_provenance_tenant_id'), 'knowledge_entity_provenance', ['tenant_id'], unique=False)
    op.create_index(op.f('ix_knowledge_entity_provenance_user_id'), 'knowledge_entity_provenance', ['user_id'], unique=False)
    op.create_index('ix_provenance_execution', 'knowledge_entity_provenance', ['execution_id'], unique=False)
    op.create_index('ix_provenance_task', 'knowledge_entity_provenance', ['task_id'], unique=False)
    op.create_index('ix_provenance_tenant_entity', 'knowledge_entity_provenance', ['tenant_id', 'entity_type', 'entity_id'], unique=False)
    op.create_index('ix_provenance_user_entity', 'knowledge_entity_provenance', ['user_id', 'entity_type', 'entity_id'], unique=False)
    op.create_table('tenant_data_management_settings',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('tenant_id', sa.Integer(), nullable=False),
    sa.Column('report_retention_enabled', sa.Boolean(), server_default='true', nullable=False),
    sa.Column('operational_log_retention_days', sa.Integer(), server_default='30', nullable=False),
    sa.Column('runner_control_retention_days', sa.Integer(), server_default='30', nullable=False),
    sa.Column('checkpoint_retention_days_after_terminal', sa.Integer(), server_default='30', nullable=False),
    sa.Column('task_retention_days_after_terminal', sa.Integer(), server_default='180', nullable=False),
    sa.Column('chat_transcript_retention_days_after_terminal', sa.Integer(), server_default='90', nullable=False),
    sa.Column('artifact_payload_retention_days', sa.Integer(), server_default='90', nullable=False),
    sa.Column('artifact_metadata_retention_days_after_terminal', sa.Integer(), server_default='180', nullable=False),
    sa.Column('report_history_retention_days', sa.Integer(), server_default='180', nullable=False),
    sa.Column('report_job_retention_days', sa.Integer(), server_default='90', nullable=False),
    sa.Column('task_memo_history_retention_days', sa.Integer(), server_default='180', nullable=False),
    sa.Column('semantic_memory_stale_retention_days', sa.Integer(), server_default='365', nullable=False),
    sa.Column('usage_record_retention_days', sa.Integer(), server_default='365', nullable=False),
    sa.Column('retention_batch_size_per_tenant', sa.Integer(), server_default='100', nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_tenant_data_management_settings_id'), 'tenant_data_management_settings', ['id'], unique=False)
    op.create_index('ix_tenant_data_management_settings_tenant_id', 'tenant_data_management_settings', ['tenant_id'], unique=True)
    op.create_table('tenant_memberships',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('tenant_id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('role', sa.String(length=32), server_default='owner', nullable=False),
    sa.Column('status', sa.String(length=32), server_default='active', nullable=False),
    sa.Column('deactivated_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('deactivated_by_user_id', sa.Integer(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.ForeignKeyConstraint(['deactivated_by_user_id'], ['users.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('tenant_id', 'user_id', name='uq_tenant_memberships_tenant_user')
    )
    op.create_index(op.f('ix_tenant_memberships_id'), 'tenant_memberships', ['id'], unique=False)
    op.create_index(op.f('ix_tenant_memberships_tenant_id'), 'tenant_memberships', ['tenant_id'], unique=False)
    op.create_index(op.f('ix_tenant_memberships_user_id'), 'tenant_memberships', ['user_id'], unique=False)
    op.create_table('user_embedding_selections',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('provider', sa.String(length=50), nullable=False),
    sa.Column('model', sa.String(length=100), nullable=False),
    sa.Column('dimensions', sa.Integer(), nullable=False),
    sa.Column('vector_family', sa.String(length=255), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('user_id', name='uq_user_embedding_selections_user_id')
    )
    op.create_index(op.f('ix_user_embedding_selections_id'), 'user_embedding_selections', ['id'], unique=False)
    op.create_index('ix_user_embedding_selections_provider_model', 'user_embedding_selections', ['provider', 'model'], unique=False)
    op.create_index(op.f('ix_user_embedding_selections_user_id'), 'user_embedding_selections', ['user_id'], unique=False)
    op.create_table('user_llm_provider_credentials',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('provider', sa.String(length=50), nullable=False),
    sa.Column('encrypted_api_key', sa.Text(), nullable=False),
    sa.Column('enabled', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('user_id', 'provider', name='uq_user_llm_provider_credentials_user_provider')
    )
    op.create_index(op.f('ix_user_llm_provider_credentials_id'), 'user_llm_provider_credentials', ['id'], unique=False)
    op.create_index('ix_user_llm_provider_credentials_provider', 'user_llm_provider_credentials', ['provider'], unique=False)
    op.create_index(op.f('ix_user_llm_provider_credentials_user_id'), 'user_llm_provider_credentials', ['user_id'], unique=False)
    op.create_table('user_llm_selections',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('provider', sa.String(length=50), nullable=False),
    sa.Column('model', sa.String(length=100), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('user_id', name='uq_user_llm_selections_user_id')
    )
    op.create_index(op.f('ix_user_llm_selections_id'), 'user_llm_selections', ['id'], unique=False)
    op.create_index('ix_user_llm_selections_provider_model', 'user_llm_selections', ['provider', 'model'], unique=False)
    op.create_index(op.f('ix_user_llm_selections_user_id'), 'user_llm_selections', ['user_id'], unique=False)
    op.create_table('user_memory_llm_selections',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('provider', sa.String(length=50), nullable=False),
    sa.Column('gate_model', sa.String(length=100), nullable=False),
    sa.Column('extraction_model', sa.String(length=100), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('user_id', name='uq_user_memory_llm_selections_user_id')
    )
    op.create_index(op.f('ix_user_memory_llm_selections_id'), 'user_memory_llm_selections', ['id'], unique=False)
    op.create_index('ix_user_memory_llm_selections_provider_models', 'user_memory_llm_selections', ['provider', 'gate_model', 'extraction_model'], unique=False)
    op.create_index(op.f('ix_user_memory_llm_selections_user_id'), 'user_memory_llm_selections', ['user_id'], unique=False)
    op.create_table('user_reporting_llm_selections',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('provider', sa.String(length=50), nullable=False),
    sa.Column('model', sa.String(length=100), nullable=False),
    sa.Column('reasoning_effort', sa.String(length=32), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('user_id', name='uq_user_reporting_llm_selections_user_id')
    )
    op.create_index(op.f('ix_user_reporting_llm_selections_id'), 'user_reporting_llm_selections', ['id'], unique=False)
    op.create_index('ix_user_reporting_llm_selections_provider_model', 'user_reporting_llm_selections', ['provider', 'model'], unique=False)
    op.create_index(op.f('ix_user_reporting_llm_selections_user_id'), 'user_reporting_llm_selections', ['user_id'], unique=False)
    op.create_table('user_sessions',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('refresh_token_hash', sa.String(length=128), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column('last_activity_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('idle_expires_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('absolute_expires_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_user_sessions_absolute_expires_at'), 'user_sessions', ['absolute_expires_at'], unique=False)
    op.create_index(op.f('ix_user_sessions_id'), 'user_sessions', ['id'], unique=False)
    op.create_index(op.f('ix_user_sessions_idle_expires_at'), 'user_sessions', ['idle_expires_at'], unique=False)
    op.create_index(op.f('ix_user_sessions_refresh_token_hash'), 'user_sessions', ['refresh_token_hash'], unique=True)
    op.create_index(op.f('ix_user_sessions_revoked_at'), 'user_sessions', ['revoked_at'], unique=False)
    op.create_index(op.f('ix_user_sessions_user_id'), 'user_sessions', ['user_id'], unique=False)
    op.create_table('user_settings',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('openai_api_key', sa.Text(), nullable=True),
    sa.Column('openai_model', sa.String(length=50), nullable=True),
    sa.Column('enable_ai', sa.Boolean(), nullable=True),
    sa.Column('shodan_api_key', sa.Text(), nullable=True),
    sa.Column('session_timeout', sa.Integer(), nullable=True),
    sa.Column('theme', sa.String(length=20), nullable=True),
    sa.Column('timezone', sa.String(length=50), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('user_id')
    )
    op.create_index(op.f('ix_user_settings_id'), 'user_settings', ['id'], unique=False)
    op.create_table('engagement_report_jobs',
    sa.Column('id', _guid_type(), nullable=False),
    sa.Column('schema_version', sa.String(length=64), server_default='1', nullable=False),
    sa.Column('tenant_id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('requested_by_user_id', sa.Integer(), nullable=False),
    sa.Column('engagement_id', sa.Integer(), nullable=False),
    sa.Column('report_id', _guid_type(), nullable=True),
    sa.Column('report_type', sa.String(length=64), nullable=False),
    sa.Column('status', sa.String(length=32), nullable=False),
    sa.Column('idempotency_key', sa.String(length=255), nullable=False),
    sa.Column('selected_task_memo_ids', sa.JSON(), nullable=False),
    sa.Column('include_candidate_findings', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('llm_runtime_selection', sa.JSON(), nullable=True),
    sa.Column('source_watermark', sa.JSON(), nullable=False),
    sa.Column('current_section_id', sa.String(length=128), nullable=True),
    sa.Column('completed_sections', sa.JSON(), nullable=False),
    sa.Column('total_sections', sa.Integer(), server_default='0', nullable=False),
    sa.Column('locked_by', sa.String(length=255), nullable=True),
    sa.Column('locked_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('attempt_count', sa.Integer(), server_default='0', nullable=False),
    sa.Column('max_attempts', sa.Integer(), server_default='3', nullable=False),
    sa.Column('last_error_code', sa.String(length=128), nullable=True),
    sa.Column('error_message', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['engagement_id'], ['engagements.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['report_id'], ['engagement_reports.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['requested_by_user_id'], ['users.id'], ),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_engagement_report_jobs_locked_at', 'engagement_report_jobs', ['locked_at'], unique=False)
    op.create_index('ix_engagement_report_jobs_status_created', 'engagement_report_jobs', ['status', 'created_at'], unique=False)
    op.create_index('ix_engagement_report_jobs_tenant_engagement_created', 'engagement_report_jobs', ['tenant_id', 'engagement_id', 'created_at'], unique=False)
    op.create_index(op.f('ix_engagement_report_jobs_tenant_id'), 'engagement_report_jobs', ['tenant_id'], unique=False)
    op.create_index('ix_engagement_report_jobs_tenant_status_created', 'engagement_report_jobs', ['tenant_id', 'status', 'created_at'], unique=False)
    op.create_index('ix_engagement_report_jobs_tenant_user_status', 'engagement_report_jobs', ['tenant_id', 'user_id', 'status'], unique=False)
    op.create_index(op.f('ix_engagement_report_jobs_user_id'), 'engagement_report_jobs', ['user_id'], unique=False)
    op.create_index('ux_engagement_report_jobs_tenant_idempotency', 'engagement_report_jobs', ['tenant_id', 'idempotency_key'], unique=True, sqlite_where=sa.text('idempotency_key IS NOT NULL'), postgresql_where=sa.text('idempotency_key IS NOT NULL'))
    op.create_table('knowledge_assets',
    sa.Column('id', _guid_type(), nullable=False),
    sa.Column('tenant_id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('engagement_id', sa.Integer(), nullable=True),
    sa.Column('asset_key', sa.String(length=512), nullable=False),
    sa.Column('asset_type', sa.String(length=120), nullable=False),
    sa.Column('display_name', sa.String(length=255), nullable=True),
    sa.Column('ip_address', sa.String(length=45), nullable=True),
    sa.Column('hostname', sa.String(length=255), nullable=True),
    sa.Column('status', sa.String(length=32), nullable=True),
    sa.Column('first_seen_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('last_seen_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('max_confidence', sa.String(length=32), nullable=True),
    sa.Column('metadata', sa.JSON(), nullable=True),
    sa.ForeignKeyConstraint(['engagement_id'], ['engagements.id'], ),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('tenant_id', 'user_id', 'asset_key', name='ux_knowledge_assets_tenant_user_asset_key')
    )
    op.create_index(op.f('ix_knowledge_assets_tenant_id'), 'knowledge_assets', ['tenant_id'], unique=False)
    op.create_index('ix_knowledge_assets_tenant_user_asset_key', 'knowledge_assets', ['tenant_id', 'user_id', 'asset_key'], unique=False)
    op.create_index('ix_knowledge_assets_tenant_user_asset_type', 'knowledge_assets', ['tenant_id', 'user_id', 'asset_type'], unique=False)
    op.create_index('ix_knowledge_assets_tenant_user_last_seen', 'knowledge_assets', ['tenant_id', 'user_id', 'last_seen_at'], unique=False)
    op.create_index(op.f('ix_knowledge_assets_user_id'), 'knowledge_assets', ['user_id'], unique=False)
    op.create_table('knowledge_evidence_archives',
    sa.Column('id', _guid_type(), nullable=False),
    sa.Column('tenant_id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('engagement_id', sa.Integer(), nullable=False),
    sa.Column('task_id', sa.Integer(), nullable=True),
    sa.Column('source_execution_id', _guid_type(), nullable=False),
    sa.Column('source_artifact_id', _guid_type(), nullable=True),
    sa.Column('storage_mode', sa.String(length=32), nullable=False),
    sa.Column('inline_excerpt', sa.Text(), nullable=True),
    sa.Column('object_key', sa.Text(), nullable=True),
    sa.Column('archived_file_ref', sa.Text(), nullable=True),
    sa.Column('content_sha256', sa.String(length=64), nullable=True),
    sa.Column('byte_size', sa.BigInteger(), nullable=True),
    sa.Column('mime_type', sa.String(length=255), nullable=True),
    sa.Column('lineage', sa.JSON(), nullable=False),
    sa.Column('metadata', sa.JSON(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.ForeignKeyConstraint(['engagement_id'], ['engagements.id'], ),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_knowledge_archives_engagement_created', 'knowledge_evidence_archives', ['engagement_id', 'created_at'], unique=False)
    op.create_index('ix_knowledge_archives_source_artifact', 'knowledge_evidence_archives', ['source_artifact_id'], unique=False)
    op.create_index('ix_knowledge_archives_source_execution', 'knowledge_evidence_archives', ['source_execution_id'], unique=False)
    op.create_index('ix_knowledge_archives_tenant_created', 'knowledge_evidence_archives', ['tenant_id', 'created_at'], unique=False)
    op.create_index('ix_knowledge_archives_tenant_object_key', 'knowledge_evidence_archives', ['tenant_id', 'object_key'], unique=False)
    op.create_index('ix_knowledge_archives_tenant_user_engagement_created', 'knowledge_evidence_archives', ['tenant_id', 'user_id', 'engagement_id', 'created_at'], unique=False)
    op.create_index('ix_knowledge_archives_user_created', 'knowledge_evidence_archives', ['user_id', 'created_at'], unique=False)
    op.create_index(op.f('ix_knowledge_evidence_archives_tenant_id'), 'knowledge_evidence_archives', ['tenant_id'], unique=False)
    op.create_index(op.f('ix_knowledge_evidence_archives_user_id'), 'knowledge_evidence_archives', ['user_id'], unique=False)
    op.create_table('knowledge_ingestion_runs',
    sa.Column('id', _guid_type(), nullable=False),
    sa.Column('tenant_id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('engagement_id', sa.Integer(), nullable=False),
    sa.Column('task_id', sa.Integer(), nullable=True),
    sa.Column('source_execution_id', _guid_type(), nullable=False),
    sa.Column('extractor_family', sa.String(length=100), nullable=False),
    sa.Column('extractor_version', sa.String(length=50), nullable=False),
    sa.Column('status', sa.String(length=32), nullable=False),
    sa.Column('error_message', sa.Text(), nullable=True),
    sa.Column('metadata', sa.JSON(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.ForeignKeyConstraint(['engagement_id'], ['engagements.id'], ),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_knowledge_ingestion_runs_tenant_id'), 'knowledge_ingestion_runs', ['tenant_id'], unique=False)
    op.create_index(op.f('ix_knowledge_ingestion_runs_user_id'), 'knowledge_ingestion_runs', ['user_id'], unique=False)
    op.create_index('ix_knowledge_runs_engagement_created', 'knowledge_ingestion_runs', ['engagement_id', 'created_at'], unique=False)
    op.create_index('ix_knowledge_runs_source_execution', 'knowledge_ingestion_runs', ['source_execution_id'], unique=False)
    op.create_index('ix_knowledge_runs_tenant_created', 'knowledge_ingestion_runs', ['tenant_id', 'created_at'], unique=False)
    op.create_index('ix_knowledge_runs_tenant_source_execution', 'knowledge_ingestion_runs', ['tenant_id', 'source_execution_id'], unique=False)
    op.create_index('ix_knowledge_runs_user_created', 'knowledge_ingestion_runs', ['user_id', 'created_at'], unique=False)
    op.create_index('ux_knowledge_runs_engagement_exec_extractor', 'knowledge_ingestion_runs', ['engagement_id', 'source_execution_id', 'extractor_family', 'extractor_version'], unique=True)
    op.create_table('knowledge_relationships',
    sa.Column('id', _guid_type(), nullable=False),
    sa.Column('tenant_id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('engagement_id', sa.Integer(), nullable=True),
    sa.Column('relationship_key', sa.String(length=512), nullable=False),
    sa.Column('source_subject_key', sa.String(length=512), nullable=False),
    sa.Column('relationship_type', sa.String(length=64), nullable=False),
    sa.Column('target_subject_key', sa.String(length=512), nullable=False),
    sa.Column('confidence', sa.String(length=32), nullable=True),
    sa.Column('first_seen_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('last_seen_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('metadata', sa.JSON(), nullable=True),
    sa.ForeignKeyConstraint(['engagement_id'], ['engagements.id'], ),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('tenant_id', 'user_id', 'relationship_key', name='ux_knowledge_relationships_tenant_user_relationship_key')
    )
    op.create_index(op.f('ix_knowledge_relationships_tenant_id'), 'knowledge_relationships', ['tenant_id'], unique=False)
    op.create_index('ix_knowledge_relationships_tenant_user_relationship_key', 'knowledge_relationships', ['tenant_id', 'user_id', 'relationship_key'], unique=False)
    op.create_index('ix_knowledge_relationships_tenant_user_source', 'knowledge_relationships', ['tenant_id', 'user_id', 'source_subject_key'], unique=False)
    op.create_index('ix_knowledge_relationships_tenant_user_target', 'knowledge_relationships', ['tenant_id', 'user_id', 'target_subject_key'], unique=False)
    op.create_index('ix_knowledge_relationships_tenant_user_type', 'knowledge_relationships', ['tenant_id', 'user_id', 'relationship_type'], unique=False)
    op.create_index(op.f('ix_knowledge_relationships_user_id'), 'knowledge_relationships', ['user_id'], unique=False)
    op.create_table('runner_install_tokens',
    sa.Column('id', _guid_type(), nullable=False),
    sa.Column('tenant_id', sa.Integer(), nullable=False),
    sa.Column('execution_site_id', _guid_type(), nullable=False),
    sa.Column('token_hash', sa.String(length=255), nullable=False),
    sa.Column('status', sa.String(length=32), server_default='issued', nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('used_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_by_user_id', sa.Integer(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.ForeignKeyConstraint(['created_by_user_id'], ['users.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['execution_site_id'], ['execution_sites.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('tenant_id', 'token_hash', name='uq_runner_install_tokens_tenant_hash')
    )
    op.create_index(op.f('ix_runner_install_tokens_created_by_user_id'), 'runner_install_tokens', ['created_by_user_id'], unique=False)
    op.create_index(op.f('ix_runner_install_tokens_execution_site_id'), 'runner_install_tokens', ['execution_site_id'], unique=False)
    op.create_index(op.f('ix_runner_install_tokens_tenant_id'), 'runner_install_tokens', ['tenant_id'], unique=False)
    op.create_index('ix_runner_install_tokens_tenant_site_status', 'runner_install_tokens', ['tenant_id', 'execution_site_id', 'status'], unique=False)
    op.create_table('runners',
    sa.Column('id', _guid_type(), nullable=False),
    sa.Column('tenant_id', sa.Integer(), nullable=False),
    sa.Column('execution_site_id', _guid_type(), nullable=False),
    sa.Column('name', sa.String(length=255), nullable=False),
    sa.Column('status', sa.String(length=32), server_default='inactive', nullable=False),
    sa.Column('version', sa.String(length=64), nullable=True),
    sa.Column('capabilities_json', sa.JSON(), nullable=True),
    sa.Column('labels_json', sa.JSON(), nullable=True),
    sa.Column('max_active_tasks', sa.Integer(), nullable=True),
    sa.Column('capacity_json', sa.JSON(), nullable=True),
    sa.Column('last_seen_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.ForeignKeyConstraint(['execution_site_id'], ['execution_sites.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('tenant_id', 'execution_site_id', 'name', name='uq_runners_tenant_site_name')
    )
    op.create_index(op.f('ix_runners_execution_site_id'), 'runners', ['execution_site_id'], unique=False)
    op.create_index(op.f('ix_runners_tenant_id'), 'runners', ['tenant_id'], unique=False)
    op.create_index('ix_runners_tenant_last_seen', 'runners', ['tenant_id', 'last_seen_at'], unique=False)
    op.create_index('ix_runners_tenant_status', 'runners', ['tenant_id', 'status'], unique=False)
    op.create_table('semantic_memories',
    sa.Column('id', _guid_type(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('tenant_id', sa.Integer(), nullable=True),
    sa.Column('engagement_id', sa.Integer(), nullable=True),
    sa.Column('task_id', sa.Integer(), nullable=True),
    sa.Column('memory_tier', sa.String(length=32), nullable=False),
    sa.Column('content', sa.Text(), nullable=False),
    sa.Column('scope_key', sa.String(length=512), nullable=False),
    sa.Column('content_hash', sa.String(length=64), nullable=False),
    sa.Column('embedding', pgvector.sqlalchemy.vector.VECTOR(dim=1536), nullable=False),
    sa.Column('embedding_provider', sa.String(length=50), server_default='openai', nullable=False),
    sa.Column('embedding_model', sa.String(length=100), server_default='text-embedding-3-small', nullable=False),
    sa.Column('embedding_dimensions', sa.Integer(), server_default='1536', nullable=False),
    sa.Column('embedding_vector_family', sa.String(length=255), server_default='openai:text-embedding-3-small:1536', nullable=False),
    sa.Column('source_type', sa.String(length=32), nullable=False),
    sa.Column('conversation_id', sa.String(length=255), nullable=True),
    sa.Column('source_turn_id', sa.String(length=255), nullable=True),
    sa.Column('metadata', sa.JSON(), nullable=True),
    sa.Column('last_accessed_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column('access_count', sa.Integer(), server_default='0', nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.CheckConstraint("(memory_tier != 'task_engagement' OR (tenant_id IS NOT NULL AND (engagement_id IS NOT NULL OR task_id IS NOT NULL)))", name='ck_semantic_memories_task_engagement_tenant_scope'),
    sa.CheckConstraint("(memory_tier != 'user_profile' OR (tenant_id IS NULL AND engagement_id IS NULL))", name='ck_semantic_memories_user_profile_private_scope'),
    sa.ForeignKeyConstraint(['engagement_id'], ['engagements.id'], ),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('scope_key', 'embedding_provider', 'embedding_model', 'embedding_dimensions', 'embedding_vector_family', name='ux_semantic_memories_scope_key_identity')
    )
    op.create_index('ix_semantic_memories_embedding_identity', 'semantic_memories', ['user_id', 'memory_tier', 'embedding_provider', 'embedding_model', 'embedding_dimensions'], unique=False)
    op.create_index(op.f('ix_semantic_memories_tenant_id'), 'semantic_memories', ['tenant_id'], unique=False)
    op.create_index('ix_semantic_memories_tenant_scope', 'semantic_memories', ['tenant_id', 'memory_tier', 'engagement_id', 'task_id'], unique=False, postgresql_where=sa.text('tenant_id IS NOT NULL'))
    op.create_index('ix_semantic_memories_user_created', 'semantic_memories', ['user_id', 'created_at'], unique=False)
    op.create_index('ix_semantic_memories_user_engagement', 'semantic_memories', ['user_id', 'engagement_id'], unique=False, postgresql_where=sa.text('engagement_id IS NOT NULL'))
    op.create_index(op.f('ix_semantic_memories_user_id'), 'semantic_memories', ['user_id'], unique=False)
    op.create_index('ix_semantic_memories_user_tier', 'semantic_memories', ['user_id', 'memory_tier'], unique=False)
    op.create_table('tasks',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('graph_thread_id', sa.String(length=64), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('tenant_id', sa.Integer(), server_default='1', nullable=False),
    sa.Column('engagement_id', sa.Integer(), nullable=True),
    sa.Column('name', sa.String(length=255), nullable=False),
    sa.Column('description', sa.Text(), nullable=True),
    sa.Column('scope', sa.Text(), nullable=True),
    sa.Column('status', sa.String(length=50), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
    sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('paused_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('stopped_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('container_id', sa.String(length=255), nullable=True),
    sa.Column('agent_pid', sa.Integer(), nullable=True),
    sa.Column('resource_usage', sa.JSON(), nullable=True),
    sa.Column('error_message', sa.Text(), nullable=True),
    sa.Column('failure_reason', sa.String(length=255), nullable=True),
    sa.Column('retry_count', sa.Integer(), nullable=True),
    sa.Column('current_step', sa.String(length=255), nullable=True),
    sa.Column('total_steps', sa.Integer(), nullable=True),
    sa.Column('progress_percentage', sa.Integer(), nullable=True),
    sa.Column('timeout_seconds', sa.Integer(), nullable=True),
    sa.Column('max_retries', sa.Integer(), nullable=True),
    sa.Column('priority', sa.Integer(), nullable=True),
    sa.Column('mode', sa.String(length=20), nullable=True),
    sa.Column('runtime_placement_mode', sa.String(length=32), server_default='local', nullable=False),
    sa.Column('runner_id', sa.String(length=255), nullable=True),
    sa.Column('execution_site_id', sa.String(length=255), nullable=True),
    sa.Column('workspace_id', sa.String(length=255), nullable=True),
    sa.Column('vpn_enabled', sa.Boolean(), nullable=True),
    sa.Column('vpn_provider', sa.String(length=50), nullable=True),
    sa.Column('vpn_config_data', sa.Text(), nullable=True),
    sa.Column('vpn_connection_status', sa.String(length=50), nullable=True),
    sa.Column('vpn_ip_address', sa.String(length=45), nullable=True),
    sa.Column('vpn_connected_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('vpn_error_message', sa.Text(), nullable=True),
    sa.ForeignKeyConstraint(['engagement_id'], ['engagements.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('graph_thread_id')
    )
    op.create_index(op.f('ix_tasks_engagement_id'), 'tasks', ['engagement_id'], unique=False)
    op.create_index(op.f('ix_tasks_id'), 'tasks', ['id'], unique=False)
    op.create_index(op.f('ix_tasks_tenant_id'), 'tasks', ['tenant_id'], unique=False)
    op.create_table('agent_logs',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('task_id', sa.Integer(), nullable=False),
    sa.Column('tenant_id', sa.Integer(), nullable=False),
    sa.Column('sequence', sa.BigInteger(), nullable=True),
    sa.Column('type', sa.String(length=20), nullable=False),
    sa.Column('content', sa.Text(), nullable=False),
    sa.Column('log_metadata', sa.JSON(), nullable=True),
    sa.Column('timestamp', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
    sa.Column('conversation_id', sa.String(length=255), nullable=True),
    sa.Column('turn_id', sa.String(length=255), nullable=False),
    sa.Column('turn_number', sa.Integer(), nullable=False),
    sa.Column('parent_event_id', sa.Integer(), nullable=True),
    sa.ForeignKeyConstraint(['parent_event_id'], ['agent_logs.id'], ),
    sa.ForeignKeyConstraint(['task_id'], ['tasks.id'], ),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('task_id', 'sequence', name='ux_agent_logs_task_sequence')
    )
    op.create_index('ix_agent_logs_conversation_id', 'agent_logs', ['conversation_id'], unique=False)
    op.create_index(op.f('ix_agent_logs_id'), 'agent_logs', ['id'], unique=False)
    op.create_index('ix_agent_logs_sequence_timestamp', 'agent_logs', ['sequence', 'timestamp'], unique=False)
    op.create_index('ix_agent_logs_task_conversation', 'agent_logs', ['task_id', 'conversation_id'], unique=False)
    op.create_index('ix_agent_logs_task_sequence', 'agent_logs', ['task_id', 'sequence'], unique=False)
    op.create_index('ix_agent_logs_task_timestamp', 'agent_logs', ['task_id', 'timestamp'], unique=False)
    op.create_index('ix_agent_logs_task_turn', 'agent_logs', ['task_id', 'turn_number'], unique=False)
    op.create_index(op.f('ix_agent_logs_tenant_id'), 'agent_logs', ['tenant_id'], unique=False)
    op.create_index('ix_agent_logs_tenant_task_sequence', 'agent_logs', ['tenant_id', 'task_id', 'sequence'], unique=False)
    op.create_index('ix_agent_logs_turn_id', 'agent_logs', ['turn_id'], unique=False)
    op.create_table('chat_messages',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('task_id', sa.Integer(), nullable=False),
    sa.Column('tenant_id', sa.Integer(), nullable=False),
    sa.Column('conversation_id', sa.String(length=255), nullable=False),
    sa.Column('turn_number', sa.Integer(), nullable=True),
    sa.Column('parent_message_id', sa.Integer(), nullable=True),
    sa.Column('latest_child_message_id', sa.Integer(), nullable=True),
    sa.Column('message_type', sa.String(length=20), nullable=False),
    sa.Column('message', sa.Text(), nullable=False),
    sa.Column('token_count', sa.Integer(), nullable=True),
    sa.Column('reasoning_tokens', sa.Text(), nullable=True),
    sa.Column('observation_tokens', sa.Text(), nullable=True),
    sa.Column('citations', sa.JSON(), nullable=True),
    sa.Column('error', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
    sa.ForeignKeyConstraint(['latest_child_message_id'], ['chat_messages.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['parent_message_id'], ['chat_messages.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['task_id'], ['tasks.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_chat_messages_created', 'chat_messages', ['created_at'], unique=False)
    op.create_index(op.f('ix_chat_messages_id'), 'chat_messages', ['id'], unique=False)
    op.create_index('ix_chat_messages_parent', 'chat_messages', ['parent_message_id'], unique=False)
    op.create_index('ix_chat_messages_task_conversation', 'chat_messages', ['task_id', 'conversation_id'], unique=False)
    op.create_index('ix_chat_messages_task_turn', 'chat_messages', ['task_id', 'turn_number'], unique=False)
    op.create_index(op.f('ix_chat_messages_tenant_id'), 'chat_messages', ['tenant_id'], unique=False)
    op.create_index('ix_chat_messages_tenant_task_created', 'chat_messages', ['tenant_id', 'task_id', 'created_at'], unique=False)
    op.create_table('engagement_asset_links',
    sa.Column('id', _guid_type(), nullable=False),
    sa.Column('tenant_id', sa.Integer(), nullable=False),
    sa.Column('engagement_id', sa.Integer(), nullable=False),
    sa.Column('asset_id', _guid_type(), nullable=False),
    sa.Column('first_seen_in_engagement', sa.DateTime(timezone=True), nullable=False),
    sa.Column('last_seen_in_engagement', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['asset_id'], ['knowledge_assets.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['engagement_id'], ['engagements.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('engagement_id', 'asset_id', name='ux_engagement_asset_links')
    )
    op.create_index('ix_engagement_asset_links_asset', 'engagement_asset_links', ['asset_id'], unique=False)
    op.create_index('ix_engagement_asset_links_engagement', 'engagement_asset_links', ['engagement_id'], unique=False)
    op.create_index('ix_engagement_asset_links_tenant_engagement', 'engagement_asset_links', ['tenant_id', 'engagement_id'], unique=False)
    op.create_index(op.f('ix_engagement_asset_links_tenant_id'), 'engagement_asset_links', ['tenant_id'], unique=False)
    op.create_table('interrupt_tickets',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('interrupt_id', sa.String(length=255), nullable=False),
    sa.Column('task_id', sa.Integer(), nullable=False),
    sa.Column('tenant_id', sa.Integer(), nullable=False),
    sa.Column('graph_name', sa.String(length=128), nullable=False),
    sa.Column('interrupt_type', sa.String(length=64), nullable=False),
    sa.Column('checkpoint_id', sa.String(length=255), nullable=True),
    sa.Column('thread_id', sa.String(length=255), nullable=True),
    sa.Column('turn_id', sa.String(length=255), nullable=True),
    sa.Column('turn_sequence', sa.Integer(), nullable=True),
    sa.Column('tool_call_id', sa.String(length=255), nullable=True),
    sa.Column('state', sa.Enum('PENDING', 'RESUMING', 'RESUMED', 'COMPLETED', 'EXPIRED', 'FAILED', name='interrupt_ticket_state', native_enum=False), nullable=False),
    sa.Column('payload_snapshot', sa.JSON(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
    sa.ForeignKeyConstraint(['task_id'], ['tasks.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('interrupt_id', name='ux_interrupt_tickets_interrupt_id')
    )
    op.create_index(op.f('ix_interrupt_tickets_id'), 'interrupt_tickets', ['id'], unique=False)
    op.create_index('ix_interrupt_tickets_interrupt_id', 'interrupt_tickets', ['interrupt_id'], unique=False)
    op.create_index('ix_interrupt_tickets_task_state', 'interrupt_tickets', ['task_id', 'state'], unique=False)
    op.create_index('ix_interrupt_tickets_task_turn_sequence', 'interrupt_tickets', ['task_id', 'turn_sequence'], unique=False)
    op.create_index(op.f('ix_interrupt_tickets_tenant_id'), 'interrupt_tickets', ['tenant_id'], unique=False)
    op.create_index('ix_interrupt_tickets_tenant_task_state', 'interrupt_tickets', ['tenant_id', 'task_id', 'state'], unique=False)
    op.create_index('ix_interrupt_tickets_tool_call_id', 'interrupt_tickets', ['tool_call_id'], unique=False)
    op.create_index('ux_interrupt_tickets_task_pending', 'interrupt_tickets', ['task_id'], unique=True, postgresql_where=sa.text("state = 'PENDING'"), sqlite_where=sa.text("state = 'PENDING'"))
    op.create_table('knowledge_observations',
    sa.Column('id', _guid_type(), nullable=False),
    sa.Column('tenant_id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('ingestion_run_id', _guid_type(), nullable=False),
    sa.Column('engagement_id', sa.Integer(), nullable=False),
    sa.Column('task_id', sa.Integer(), nullable=True),
    sa.Column('source_execution_id', _guid_type(), nullable=False),
    sa.Column('observation_type', sa.String(length=120), nullable=False),
    sa.Column('subject_type', sa.String(length=120), nullable=False),
    sa.Column('subject_key', sa.String(length=512), nullable=False),
    sa.Column('assertion_level', sa.String(length=32), nullable=False),
    sa.Column('dedupe_key', sa.String(length=64), nullable=False),
    sa.Column('payload', sa.JSON(), nullable=False),
    sa.Column('observed_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('metadata', sa.JSON(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.ForeignKeyConstraint(['engagement_id'], ['engagements.id'], ),
    sa.ForeignKeyConstraint(['ingestion_run_id'], ['knowledge_ingestion_runs.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('ingestion_run_id', 'dedupe_key', name='ux_knowledge_observations_run_dedupe')
    )
    op.create_index('ix_knowledge_observations_engagement_created', 'knowledge_observations', ['engagement_id', 'created_at'], unique=False)
    op.create_index('ix_knowledge_observations_engagement_subject', 'knowledge_observations', ['engagement_id', 'subject_type', 'subject_key'], unique=False)
    op.create_index('ix_knowledge_observations_source_execution', 'knowledge_observations', ['source_execution_id'], unique=False)
    op.create_index('ix_knowledge_observations_tenant_created', 'knowledge_observations', ['tenant_id', 'created_at'], unique=False)
    op.create_index(op.f('ix_knowledge_observations_tenant_id'), 'knowledge_observations', ['tenant_id'], unique=False)
    op.create_index('ix_knowledge_observations_tenant_source_execution', 'knowledge_observations', ['tenant_id', 'source_execution_id'], unique=False)
    op.create_index('ix_knowledge_observations_user_created', 'knowledge_observations', ['user_id', 'created_at'], unique=False)
    op.create_index(op.f('ix_knowledge_observations_user_id'), 'knowledge_observations', ['user_id'], unique=False)
    op.create_table('knowledge_services',
    sa.Column('id', _guid_type(), nullable=False),
    sa.Column('tenant_id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('engagement_id', sa.Integer(), nullable=True),
    sa.Column('service_key', sa.String(length=512), nullable=False),
    sa.Column('asset_id', _guid_type(), nullable=True),
    sa.Column('protocol', sa.String(length=16), nullable=True),
    sa.Column('port', sa.Integer(), nullable=True),
    sa.Column('service_name', sa.String(length=255), nullable=True),
    sa.Column('product', sa.String(length=255), nullable=True),
    sa.Column('version', sa.String(length=120), nullable=True),
    sa.Column('status', sa.String(length=32), nullable=True),
    sa.Column('first_seen_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('last_seen_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('metadata', sa.JSON(), nullable=True),
    sa.ForeignKeyConstraint(['asset_id'], ['knowledge_assets.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['engagement_id'], ['engagements.id'], ),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('tenant_id', 'user_id', 'service_key', name='ux_knowledge_services_tenant_user_service_key')
    )
    op.create_index(op.f('ix_knowledge_services_tenant_id'), 'knowledge_services', ['tenant_id'], unique=False)
    op.create_index('ix_knowledge_services_tenant_user_asset', 'knowledge_services', ['tenant_id', 'user_id', 'asset_id'], unique=False)
    op.create_index('ix_knowledge_services_tenant_user_last_seen', 'knowledge_services', ['tenant_id', 'user_id', 'last_seen_at'], unique=False)
    op.create_index('ix_knowledge_services_tenant_user_service_key', 'knowledge_services', ['tenant_id', 'user_id', 'service_key'], unique=False)
    op.create_index(op.f('ix_knowledge_services_user_id'), 'knowledge_services', ['user_id'], unique=False)
    op.create_table('llm_conversations',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('task_id', sa.Integer(), nullable=False),
    sa.Column('tenant_id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('provider', sa.String(length=50), nullable=False),
    sa.Column('model', sa.String(length=100), nullable=True),
    sa.Column('conversation_id', sa.String(length=255), nullable=True),
    sa.Column('title', sa.String(length=255), nullable=True),
    sa.Column('status', sa.String(length=32), nullable=True),
    sa.Column('is_active', sa.Boolean(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
    sa.ForeignKeyConstraint(['task_id'], ['tasks.id'], ),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_llm_conversations_id'), 'llm_conversations', ['id'], unique=False)
    op.create_index(op.f('ix_llm_conversations_task_id'), 'llm_conversations', ['task_id'], unique=False)
    op.create_index('ix_llm_conversations_task_user_provider', 'llm_conversations', ['task_id', 'user_id', 'provider'], unique=False)
    op.create_index(op.f('ix_llm_conversations_tenant_id'), 'llm_conversations', ['tenant_id'], unique=False)
    op.create_index('ix_llm_conversations_tenant_task_created', 'llm_conversations', ['tenant_id', 'task_id', 'created_at'], unique=False)
    op.create_index(op.f('ix_llm_conversations_user_id'), 'llm_conversations', ['user_id'], unique=False)
    op.create_table('llm_usage_records',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('task_id', sa.Integer(), nullable=False),
    sa.Column('tenant_id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('prompt_tokens', sa.Integer(), nullable=False),
    sa.Column('completion_tokens', sa.Integer(), nullable=False),
    sa.Column('total_tokens', sa.Integer(), nullable=False),
    sa.Column('cached_tokens', sa.Integer(), nullable=False),
    sa.Column('reasoning_tokens', sa.Integer(), nullable=False),
    sa.Column('model', sa.String(length=100), nullable=False),
    sa.Column('provider', sa.String(length=50), nullable=False),
    sa.Column('source', sa.String(length=50), nullable=False),
    sa.Column('conversation_id', sa.String(length=255), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
    sa.Column('request_metadata', sa.JSON(), nullable=True),
    sa.ForeignKeyConstraint(['task_id'], ['tasks.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_llm_usage_records_id'), 'llm_usage_records', ['id'], unique=False)
    op.create_index(op.f('ix_llm_usage_records_task_id'), 'llm_usage_records', ['task_id'], unique=False)
    op.create_index(op.f('ix_llm_usage_records_tenant_id'), 'llm_usage_records', ['tenant_id'], unique=False)
    op.create_index(op.f('ix_llm_usage_records_user_id'), 'llm_usage_records', ['user_id'], unique=False)
    op.create_index('ix_llm_usage_task_created', 'llm_usage_records', ['task_id', 'created_at'], unique=False)
    op.create_index('ix_llm_usage_task_model', 'llm_usage_records', ['task_id', 'model'], unique=False)
    op.create_index('ix_llm_usage_tenant_task_created', 'llm_usage_records', ['tenant_id', 'task_id', 'created_at'], unique=False)
    op.create_index('ix_llm_usage_user_created', 'llm_usage_records', ['user_id', 'created_at'], unique=False)
    op.create_table('reports',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('task_id', sa.Integer(), nullable=False),
    sa.Column('tenant_id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('title', sa.String(length=255), nullable=False),
    sa.Column('content', sa.Text(), nullable=False),
    sa.Column('findings', sa.JSON(), nullable=True),
    sa.Column('severity', sa.String(length=20), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
    sa.ForeignKeyConstraint(['task_id'], ['tasks.id'], ),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_reports_id'), 'reports', ['id'], unique=False)
    op.create_index(op.f('ix_reports_tenant_id'), 'reports', ['tenant_id'], unique=False)
    op.create_index('ix_reports_tenant_task_created', 'reports', ['tenant_id', 'task_id', 'created_at'], unique=False)
    op.create_table('runner_connections',
    sa.Column('id', _guid_type(), nullable=False),
    sa.Column('tenant_id', sa.Integer(), nullable=False),
    sa.Column('runner_id', _guid_type(), nullable=False),
    sa.Column('pod_id', sa.String(length=255), nullable=False),
    sa.Column('connection_id', sa.String(length=255), nullable=False),
    sa.Column('status', sa.String(length=32), server_default='active', nullable=False),
    sa.Column('lease_expires_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('last_seen_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.ForeignKeyConstraint(['runner_id'], ['runners.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('tenant_id', 'runner_id', 'pod_id', 'connection_id', name='uq_runner_connections_tenant_runner_pod_connection')
    )
    op.create_index(op.f('ix_runner_connections_runner_id'), 'runner_connections', ['runner_id'], unique=False)
    op.create_index(op.f('ix_runner_connections_tenant_id'), 'runner_connections', ['tenant_id'], unique=False)
    op.create_index('ix_runner_connections_tenant_runner_last_seen', 'runner_connections', ['tenant_id', 'runner_id', 'last_seen_at'], unique=False)
    op.create_index('ix_runner_connections_tenant_status', 'runner_connections', ['tenant_id', 'status'], unique=False)
    op.create_table('runner_credentials',
    sa.Column('id', _guid_type(), nullable=False),
    sa.Column('tenant_id', sa.Integer(), nullable=False),
    sa.Column('runner_id', _guid_type(), nullable=False),
    sa.Column('credential_fingerprint', sa.String(length=128), nullable=False),
    sa.Column('secret_hash', sa.String(length=255), nullable=False),
    sa.Column('status', sa.String(length=32), server_default='active', nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_used_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.ForeignKeyConstraint(['runner_id'], ['runners.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('tenant_id', 'credential_fingerprint', name='uq_runner_credentials_tenant_fingerprint')
    )
    op.create_index(op.f('ix_runner_credentials_runner_id'), 'runner_credentials', ['runner_id'], unique=False)
    op.create_index(op.f('ix_runner_credentials_tenant_id'), 'runner_credentials', ['tenant_id'], unique=False)
    op.create_index('ix_runner_credentials_tenant_runner', 'runner_credentials', ['tenant_id', 'runner_id'], unique=False)
    op.create_table('runtime_jobs',
    sa.Column('id', _guid_type(), nullable=False),
    sa.Column('tenant_id', sa.Integer(), nullable=False),
    sa.Column('task_id', sa.Integer(), nullable=True),
    sa.Column('runner_id', _guid_type(), nullable=True),
    sa.Column('execution_site_id', _guid_type(), nullable=True),
    sa.Column('job_type', sa.String(length=64), nullable=False),
    sa.Column('status', sa.String(length=32), server_default='queued', nullable=False),
    sa.Column('idempotency_key', sa.String(length=255), nullable=False),
    sa.Column('correlation_id', sa.String(length=255), nullable=True),
    sa.Column('payload_json', sa.JSON(), nullable=True),
    sa.Column('result_json', sa.JSON(), nullable=True),
    sa.Column('error_code', sa.String(length=128), nullable=True),
    sa.Column('error_message', sa.Text(), nullable=True),
    sa.Column('lease_expires_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.ForeignKeyConstraint(['execution_site_id'], ['execution_sites.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['runner_id'], ['runners.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['task_id'], ['tasks.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('tenant_id', 'job_type', 'idempotency_key', name='uq_runtime_jobs_tenant_type_idempotency')
    )
    op.create_index(op.f('ix_runtime_jobs_execution_site_id'), 'runtime_jobs', ['execution_site_id'], unique=False)
    op.create_index(op.f('ix_runtime_jobs_runner_id'), 'runtime_jobs', ['runner_id'], unique=False)
    op.create_index(op.f('ix_runtime_jobs_task_id'), 'runtime_jobs', ['task_id'], unique=False)
    op.create_index(op.f('ix_runtime_jobs_tenant_id'), 'runtime_jobs', ['tenant_id'], unique=False)
    op.create_index('ix_runtime_jobs_tenant_runner_status', 'runtime_jobs', ['tenant_id', 'runner_id', 'status'], unique=False)
    op.create_index('ix_runtime_jobs_tenant_status', 'runtime_jobs', ['tenant_id', 'status'], unique=False)
    op.create_table('stream_events',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('task_id', sa.Integer(), nullable=False),
    sa.Column('tenant_id', sa.Integer(), nullable=False),
    sa.Column('sequence', sa.BigInteger(), nullable=False),
    sa.Column('event_type', sa.String(length=50), nullable=True),
    sa.Column('conversation_id', sa.String(length=255), nullable=True),
    sa.Column('turn_id', sa.String(length=255), nullable=True),
    sa.Column('payload', sa.JSON(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
    sa.ForeignKeyConstraint(['task_id'], ['tasks.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('task_id', 'sequence', name='ux_stream_events_task_sequence')
    )
    op.create_index(op.f('ix_stream_events_id'), 'stream_events', ['id'], unique=False)
    op.create_index('ix_stream_events_task_conversation', 'stream_events', ['task_id', 'conversation_id'], unique=False)
    op.create_index('ix_stream_events_task_sequence', 'stream_events', ['task_id', 'sequence'], unique=False)
    op.create_index('ix_stream_events_task_turn', 'stream_events', ['task_id', 'turn_id'], unique=False)
    op.create_index(op.f('ix_stream_events_tenant_id'), 'stream_events', ['tenant_id'], unique=False)
    op.create_index('ix_stream_events_tenant_task_sequence', 'stream_events', ['tenant_id', 'task_id', 'sequence'], unique=False)
    op.create_index('ix_stream_events_timestamp', 'stream_events', ['created_at'], unique=False)
    op.create_table('system_logs',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('task_id', sa.Integer(), nullable=False),
    sa.Column('tenant_id', sa.Integer(), nullable=False),
    sa.Column('sequence', sa.BigInteger(), nullable=False),
    sa.Column('type', sa.String(length=50), nullable=False),
    sa.Column('content', sa.Text(), nullable=True),
    sa.Column('log_metadata', sa.JSON(), nullable=True),
    sa.Column('timestamp', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
    sa.ForeignKeyConstraint(['task_id'], ['tasks.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('task_id', 'sequence', name='ux_system_logs_task_sequence')
    )
    op.create_index(op.f('ix_system_logs_id'), 'system_logs', ['id'], unique=False)
    op.create_index('ix_system_logs_task_sequence', 'system_logs', ['task_id', 'sequence'], unique=False)
    op.create_index(op.f('ix_system_logs_tenant_id'), 'system_logs', ['tenant_id'], unique=False)
    op.create_index('ix_system_logs_tenant_task_sequence', 'system_logs', ['tenant_id', 'task_id', 'sequence'], unique=False)
    op.create_index('ix_system_logs_timestamp', 'system_logs', ['timestamp'], unique=False)
    op.create_table('task_closure_memos',
    sa.Column('id', _guid_type(), nullable=False),
    sa.Column('schema_version', sa.String(length=64), server_default='1', nullable=False),
    sa.Column('tenant_id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('created_by_user_id', sa.Integer(), nullable=False),
    sa.Column('engagement_id', sa.Integer(), nullable=False),
    sa.Column('task_id', sa.Integer(), nullable=False),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.Column('is_current', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('status', sa.String(length=32), nullable=False),
    sa.Column('memo_mode', sa.String(length=32), nullable=False),
    sa.Column('source_watermark', sa.JSON(), nullable=False),
    sa.Column('memo', sa.JSON(), nullable=False),
    sa.Column('generation_metadata', sa.JSON(), nullable=True),
    sa.Column('error_message', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column('generated_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['created_by_user_id'], ['users.id'], ),
    sa.ForeignKeyConstraint(['engagement_id'], ['engagements.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['task_id'], ['tasks.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_task_closure_memos_tenant_engagement_status', 'task_closure_memos', ['tenant_id', 'engagement_id', 'status'], unique=False)
    op.create_index('ix_task_closure_memos_tenant_engagement_task', 'task_closure_memos', ['tenant_id', 'engagement_id', 'task_id'], unique=False)
    op.create_index(op.f('ix_task_closure_memos_tenant_id'), 'task_closure_memos', ['tenant_id'], unique=False)
    op.create_index('ix_task_closure_memos_tenant_user_engagement_task_current', 'task_closure_memos', ['tenant_id', 'user_id', 'engagement_id', 'task_id', 'is_current'], unique=False)
    op.create_index('ix_task_closure_memos_tenant_user_updated', 'task_closure_memos', ['tenant_id', 'user_id', 'updated_at'], unique=False)
    op.create_index(op.f('ix_task_closure_memos_user_id'), 'task_closure_memos', ['user_id'], unique=False)
    op.create_index('ux_task_closure_memos_current_ready', 'task_closure_memos', ['tenant_id', 'user_id', 'engagement_id', 'task_id'], unique=True, sqlite_where=sa.text("status = 'ready' AND is_current IS true"), postgresql_where=sa.text("status = 'ready' AND is_current IS true"))
    op.create_index('ux_task_closure_memos_preparing', 'task_closure_memos', ['tenant_id', 'user_id', 'engagement_id', 'task_id'], unique=True, sqlite_where=sa.text("status = 'preparing'"), postgresql_where=sa.text("status = 'preparing'"))
    op.create_index('ux_task_closure_memos_version', 'task_closure_memos', ['tenant_id', 'user_id', 'engagement_id', 'task_id', 'version'], unique=True)
    op.create_table('task_history',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('task_id', sa.Integer(), nullable=False),
    sa.Column('tenant_id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=True),
    sa.Column('old_status', sa.String(length=50), nullable=True),
    sa.Column('new_status', sa.String(length=50), nullable=False),
    sa.Column('transition_reason', sa.Text(), nullable=True),
    sa.Column('timestamp', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
    sa.Column('change_source', sa.String(length=50), nullable=True),
    sa.Column('change_metadata', sa.JSON(), nullable=True),
    sa.ForeignKeyConstraint(['task_id'], ['tasks.id'], ),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_task_history_id'), 'task_history', ['id'], unique=False)
    op.create_index(op.f('ix_task_history_tenant_id'), 'task_history', ['tenant_id'], unique=False)
    op.create_index('ix_task_history_tenant_task_timestamp', 'task_history', ['tenant_id', 'task_id', 'timestamp'], unique=False)
    op.create_table('turn_workflows',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('task_id', sa.Integer(), nullable=False),
    sa.Column('tenant_id', sa.Integer(), nullable=False),
    sa.Column('conversation_id', sa.String(length=255), nullable=False),
    sa.Column('turn_id', sa.String(length=255), nullable=False),
    sa.Column('turn_sequence', sa.Integer(), nullable=True),
    sa.Column('state', sa.String(length=64), nullable=False),
    sa.Column('graph_name', sa.String(length=128), nullable=True),
    sa.Column('checkpoint_id', sa.String(length=255), nullable=True),
    sa.Column('interrupt_type', sa.String(length=64), nullable=True),
    sa.Column('reserved_message_id', sa.Integer(), nullable=True),
    sa.Column('resume_key', sa.String(length=255), nullable=True),
    sa.Column('workflow_metadata', sa.JSON(), nullable=True),
    sa.Column('waiting_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('resumed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('failed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
    sa.ForeignKeyConstraint(['task_id'], ['tasks.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('task_id', 'turn_id', name='ux_turn_workflows_task_turn_id')
    )
    op.create_index(op.f('ix_turn_workflows_id'), 'turn_workflows', ['id'], unique=False)
    op.create_index('ix_turn_workflows_task_checkpoint', 'turn_workflows', ['task_id', 'checkpoint_id'], unique=False)
    op.create_index('ix_turn_workflows_task_resume_key', 'turn_workflows', ['task_id', 'resume_key'], unique=False)
    op.create_index('ix_turn_workflows_task_state', 'turn_workflows', ['task_id', 'state'], unique=False)
    op.create_index('ix_turn_workflows_task_turn_sequence', 'turn_workflows', ['task_id', 'turn_sequence'], unique=False)
    op.create_index(op.f('ix_turn_workflows_tenant_id'), 'turn_workflows', ['tenant_id'], unique=False)
    op.create_index('ix_turn_workflows_tenant_task_turn_sequence', 'turn_workflows', ['tenant_id', 'task_id', 'turn_sequence'], unique=False)
    op.create_table('artifact_manifests',
    sa.Column('id', _guid_type(), nullable=False),
    sa.Column('tenant_id', sa.Integer(), nullable=False),
    sa.Column('task_id', sa.Integer(), nullable=False),
    sa.Column('runtime_job_id', _guid_type(), nullable=True),
    sa.Column('runner_id', _guid_type(), nullable=True),
    sa.Column('command_id', sa.String(length=255), nullable=False),
    sa.Column('workspace_id', sa.String(length=255), nullable=False),
    sa.Column('message_id', sa.String(length=255), nullable=False),
    sa.Column('idempotency_key', sa.String(length=255), nullable=True),
    sa.Column('status', sa.String(length=32), server_default='accepted', nullable=False),
    sa.Column('manifest_json', sa.JSON(), nullable=True),
    sa.Column('metadata', sa.JSON(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.ForeignKeyConstraint(['runner_id'], ['runners.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['runtime_job_id'], ['runtime_jobs.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['task_id'], ['tasks.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_artifact_manifests_runner_id'), 'artifact_manifests', ['runner_id'], unique=False)
    op.create_index(op.f('ix_artifact_manifests_runtime_job_id'), 'artifact_manifests', ['runtime_job_id'], unique=False)
    op.create_index(op.f('ix_artifact_manifests_task_id'), 'artifact_manifests', ['task_id'], unique=False)
    op.create_index(op.f('ix_artifact_manifests_tenant_id'), 'artifact_manifests', ['tenant_id'], unique=False)
    op.create_index('ix_artifact_manifests_tenant_idempotency', 'artifact_manifests', ['tenant_id', 'idempotency_key'], unique=False)
    op.create_index('ix_artifact_manifests_tenant_runtime_job', 'artifact_manifests', ['tenant_id', 'runtime_job_id'], unique=False)
    op.create_index('ix_artifact_manifests_tenant_task_created', 'artifact_manifests', ['tenant_id', 'task_id', 'created_at'], unique=False)
    op.create_index('ux_artifact_manifests_tenant_runtime_command_workspace_message', 'artifact_manifests', ['tenant_id', 'runtime_job_id', 'command_id', 'workspace_id', 'message_id'], unique=True)
    op.create_table('chat_turn_events',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('task_id', sa.Integer(), nullable=False),
    sa.Column('tenant_id', sa.Integer(), nullable=False),
    sa.Column('conversation_id', sa.String(length=255), nullable=False),
    sa.Column('chat_message_id', sa.Integer(), nullable=False),
    sa.Column('turn_number', sa.Integer(), nullable=False),
    sa.Column('phase_sequence', sa.Integer(), nullable=False),
    sa.Column('kind', sa.String(length=32), nullable=False),
    sa.Column('sub_turn_index', sa.Integer(), nullable=True),
    sa.Column('tool_call_id', sa.String(length=255), nullable=True),
    sa.Column('content', sa.Text(), nullable=True),
    sa.Column('metadata', sa.JSON(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.ForeignKeyConstraint(['chat_message_id'], ['chat_messages.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['task_id'], ['tasks.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('chat_message_id', 'phase_sequence', name='ux_chat_turn_events_message_phase_sequence')
    )
    op.create_index(op.f('ix_chat_turn_events_id'), 'chat_turn_events', ['id'], unique=False)
    op.create_index('ix_chat_turn_events_task_conv_turn_phase', 'chat_turn_events', ['task_id', 'conversation_id', 'turn_number', 'phase_sequence'], unique=False)
    op.create_index(op.f('ix_chat_turn_events_tenant_id'), 'chat_turn_events', ['tenant_id'], unique=False)
    op.create_index('ix_chat_turn_events_tenant_task_created', 'chat_turn_events', ['tenant_id', 'task_id', 'created_at'], unique=False)
    op.create_table('engagement_service_links',
    sa.Column('id', _guid_type(), nullable=False),
    sa.Column('tenant_id', sa.Integer(), nullable=False),
    sa.Column('engagement_id', sa.Integer(), nullable=False),
    sa.Column('service_id', _guid_type(), nullable=False),
    sa.Column('first_seen_in_engagement', sa.DateTime(timezone=True), nullable=False),
    sa.Column('last_seen_in_engagement', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['engagement_id'], ['engagements.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['service_id'], ['knowledge_services.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('engagement_id', 'service_id', name='ux_engagement_service_links')
    )
    op.create_index('ix_engagement_service_links_engagement', 'engagement_service_links', ['engagement_id'], unique=False)
    op.create_index('ix_engagement_service_links_service', 'engagement_service_links', ['service_id'], unique=False)
    op.create_index('ix_engagement_service_links_tenant_engagement', 'engagement_service_links', ['tenant_id', 'engagement_id'], unique=False)
    op.create_index(op.f('ix_engagement_service_links_tenant_id'), 'engagement_service_links', ['tenant_id'], unique=False)
    op.create_table('knowledge_findings',
    sa.Column('id', _guid_type(), nullable=False),
    sa.Column('tenant_id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('engagement_id', sa.Integer(), nullable=True),
    sa.Column('finding_key', sa.String(length=512), nullable=False),
    sa.Column('finding_type', sa.String(length=120), nullable=False),
    sa.Column('subject_type', sa.String(length=120), nullable=False),
    sa.Column('subject_key', sa.String(length=512), nullable=False),
    sa.Column('asset_id', _guid_type(), nullable=True),
    sa.Column('service_id', _guid_type(), nullable=True),
    sa.Column('title', sa.Text(), nullable=True),
    sa.Column('severity', sa.String(length=32), nullable=True),
    sa.Column('status', sa.String(length=32), nullable=True),
    sa.Column('assertion_level', sa.String(length=32), nullable=True),
    sa.Column('confidence', sa.String(length=32), nullable=True),
    sa.Column('first_seen_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('last_seen_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('evidence_summary', sa.JSON(), nullable=True),
    sa.Column('metadata', sa.JSON(), nullable=True),
    sa.ForeignKeyConstraint(['asset_id'], ['knowledge_assets.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['engagement_id'], ['engagements.id'], ),
    sa.ForeignKeyConstraint(['service_id'], ['knowledge_services.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('tenant_id', 'user_id', 'finding_key', name='ux_knowledge_findings_tenant_user_finding_key')
    )
    op.create_index(op.f('ix_knowledge_findings_tenant_id'), 'knowledge_findings', ['tenant_id'], unique=False)
    op.create_index('ix_knowledge_findings_tenant_user_asset', 'knowledge_findings', ['tenant_id', 'user_id', 'asset_id'], unique=False)
    op.create_index('ix_knowledge_findings_tenant_user_finding_key', 'knowledge_findings', ['tenant_id', 'user_id', 'finding_key'], unique=False)
    op.create_index('ix_knowledge_findings_tenant_user_service', 'knowledge_findings', ['tenant_id', 'user_id', 'service_id'], unique=False)
    op.create_index('ix_knowledge_findings_tenant_user_status', 'knowledge_findings', ['tenant_id', 'user_id', 'status'], unique=False)
    op.create_index(op.f('ix_knowledge_findings_user_id'), 'knowledge_findings', ['user_id'], unique=False)
    op.create_table('knowledge_web_paths',
    sa.Column('id', _guid_type(), nullable=False),
    sa.Column('tenant_id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('asset_id', _guid_type(), nullable=True),
    sa.Column('service_id', _guid_type(), nullable=True),
    sa.Column('canonical_url', sa.String(length=1024), nullable=False),
    sa.Column('origin_key', sa.String(length=512), nullable=False),
    sa.Column('path', sa.String(length=1024), nullable=False),
    sa.Column('last_status_code', sa.Integer(), nullable=True),
    sa.Column('last_response_size', sa.BigInteger(), nullable=True),
    sa.Column('calibrated_baseline', sa.Boolean(), nullable=False),
    sa.Column('noise_score', sa.Float(), nullable=False),
    sa.Column('first_seen_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('last_seen_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('producer_summary', sa.JSON(), nullable=False),
    sa.Column('evidence_refs', sa.JSON(), nullable=False),
    sa.ForeignKeyConstraint(['asset_id'], ['knowledge_assets.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['service_id'], ['knowledge_services.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('tenant_id', 'user_id', 'canonical_url', name='ux_knowledge_web_paths_tenant_user_url')
    )
    op.create_index(op.f('ix_knowledge_web_paths_tenant_id'), 'knowledge_web_paths', ['tenant_id'], unique=False)
    op.create_index('ix_knowledge_web_paths_tenant_user_asset', 'knowledge_web_paths', ['tenant_id', 'user_id', 'asset_id'], unique=False)
    op.create_index('ix_knowledge_web_paths_tenant_user_last_seen', 'knowledge_web_paths', ['tenant_id', 'user_id', 'last_seen_at'], unique=False)
    op.create_index('ix_knowledge_web_paths_tenant_user_origin', 'knowledge_web_paths', ['tenant_id', 'user_id', 'origin_key'], unique=False)
    op.create_index('ix_knowledge_web_paths_tenant_user_service', 'knowledge_web_paths', ['tenant_id', 'user_id', 'service_id'], unique=False)
    op.create_index('ix_knowledge_web_paths_tenant_user_url', 'knowledge_web_paths', ['tenant_id', 'user_id', 'canonical_url'], unique=False)
    op.create_index(op.f('ix_knowledge_web_paths_user_id'), 'knowledge_web_paths', ['user_id'], unique=False)
    op.create_table('runner_control_messages',
    sa.Column('id', _guid_type(), nullable=False),
    sa.Column('tenant_id', sa.Integer(), nullable=False),
    sa.Column('runner_id', _guid_type(), nullable=False),
    sa.Column('runtime_job_id', _guid_type(), nullable=True),
    sa.Column('task_id', sa.Integer(), nullable=True),
    sa.Column('message_id', sa.String(length=255), nullable=False),
    sa.Column('direction', sa.String(length=16), nullable=False),
    sa.Column('type', sa.String(length=64), nullable=False),
    sa.Column('status', sa.String(length=32), server_default='pending', nullable=False),
    sa.Column('idempotency_key', sa.String(length=255), nullable=True),
    sa.Column('correlation_id', sa.String(length=255), nullable=True),
    sa.Column('payload_json', sa.JSON(), nullable=True),
    sa.Column('delivery_attempt_count', sa.Integer(), server_default='0', nullable=False),
    sa.Column('error_code', sa.String(length=128), nullable=True),
    sa.Column('error_message', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.ForeignKeyConstraint(['runner_id'], ['runners.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['runtime_job_id'], ['runtime_jobs.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['task_id'], ['tasks.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('tenant_id', 'runner_id', 'direction', 'message_id', name='uq_runner_control_messages_tenant_runner_direction_message')
    )
    op.create_index(op.f('ix_runner_control_messages_runner_id'), 'runner_control_messages', ['runner_id'], unique=False)
    op.create_index('ix_runner_control_messages_runtime_job', 'runner_control_messages', ['runtime_job_id'], unique=False)
    op.create_index(op.f('ix_runner_control_messages_runtime_job_id'), 'runner_control_messages', ['runtime_job_id'], unique=False)
    op.create_index(op.f('ix_runner_control_messages_task_id'), 'runner_control_messages', ['task_id'], unique=False)
    op.create_index(op.f('ix_runner_control_messages_tenant_id'), 'runner_control_messages', ['tenant_id'], unique=False)
    op.create_index('ix_runner_control_messages_tenant_status', 'runner_control_messages', ['tenant_id', 'status'], unique=False)
    op.create_index('uq_runner_control_messages_inbound_idempotency', 'runner_control_messages', ['tenant_id', 'runner_id', 'idempotency_key'], unique=True, postgresql_where=sa.text("direction = 'inbound' AND idempotency_key IS NOT NULL"), sqlite_where=sa.text("direction = 'inbound' AND idempotency_key IS NOT NULL"))
    op.create_index('uq_runner_control_messages_outbound_idempotency', 'runner_control_messages', ['tenant_id', 'runner_id', 'idempotency_key'], unique=True, postgresql_where=sa.text("direction = 'outbound' AND idempotency_key IS NOT NULL"), sqlite_where=sa.text("direction = 'outbound' AND idempotency_key IS NOT NULL"))
    op.create_table('tool_calls',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('chat_message_id', sa.Integer(), nullable=False),
    sa.Column('tenant_id', sa.Integer(), nullable=False),
    sa.Column('parent_tool_call_id', sa.Integer(), nullable=True),
    sa.Column('tool_call_id', sa.String(length=255), nullable=False),
    sa.Column('tool_id', sa.Integer(), nullable=True),
    sa.Column('tool_name', sa.String(length=255), nullable=False),
    sa.Column('tool_arguments', sa.JSON(), nullable=False),
    sa.Column('tool_result', sa.Text(), nullable=True),
    sa.Column('turn_index', sa.Integer(), nullable=False),
    sa.Column('tab_index', sa.Integer(), nullable=True),
    sa.Column('reasoning_tokens', sa.Text(), nullable=True),
    sa.Column('generated_images', sa.JSON(), nullable=True),
    sa.Column('tool_call_tokens', sa.Integer(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
    sa.ForeignKeyConstraint(['chat_message_id'], ['chat_messages.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['parent_tool_call_id'], ['tool_calls.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('chat_message_id', 'tool_call_id', name='ux_tool_calls_chat_message_tool_call_id')
    )
    op.create_index(op.f('ix_tool_calls_id'), 'tool_calls', ['id'], unique=False)
    op.create_index('ix_tool_calls_message', 'tool_calls', ['chat_message_id'], unique=False)
    op.create_index('ix_tool_calls_parent', 'tool_calls', ['parent_tool_call_id'], unique=False)
    op.create_index(op.f('ix_tool_calls_tenant_id'), 'tool_calls', ['tenant_id'], unique=False)
    op.create_index('ix_tool_calls_tenant_message_created', 'tool_calls', ['tenant_id', 'chat_message_id', 'created_at'], unique=False)
    op.create_table('tool_executions',
    sa.Column('id', _guid_type(), nullable=False),
    sa.Column('tenant_id', sa.Integer(), nullable=False),
    sa.Column('task_id', sa.Integer(), nullable=False),
    sa.Column('runtime_job_id', _guid_type(), nullable=True),
    sa.Column('runner_id', _guid_type(), nullable=True),
    sa.Column('execution_site_id', _guid_type(), nullable=True),
    sa.Column('command_id', sa.String(length=255), nullable=True),
    sa.Column('workspace_id', sa.String(length=255), nullable=True),
    sa.Column('chat_message_id', sa.Integer(), nullable=True),
    sa.Column('tool_call_id', sa.String(length=255), nullable=True),
    sa.Column('conversation_id', sa.String(length=255), nullable=True),
    sa.Column('turn_id', sa.String(length=255), nullable=True),
    sa.Column('turn_sequence', sa.Integer(), nullable=True),
    sa.Column('tool_name', sa.String(length=255), nullable=False),
    sa.Column('tool_arguments', sa.JSON(), nullable=False),
    sa.Column('purpose', sa.Text(), nullable=True),
    sa.Column('agent_path', sa.String(length=50), nullable=False),
    sa.Column('execution_transport', sa.String(length=50), nullable=True),
    sa.Column('workspace_path', sa.Text(), nullable=True),
    sa.Column('container_path', sa.Text(), nullable=True),
    sa.Column('status', sa.String(length=50), nullable=False),
    sa.Column('exit_code', sa.Integer(), nullable=True),
    sa.Column('started_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('duration_ms', sa.Integer(), nullable=True),
    sa.Column('metadata', sa.JSON(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.ForeignKeyConstraint(['chat_message_id'], ['chat_messages.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['execution_site_id'], ['execution_sites.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['runner_id'], ['runners.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['runtime_job_id'], ['runtime_jobs.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['task_id'], ['tasks.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_tool_executions_chat_message', 'tool_executions', ['chat_message_id'], unique=False)
    op.create_index(op.f('ix_tool_executions_command_id'), 'tool_executions', ['command_id'], unique=False)
    op.create_index('ix_tool_executions_conversation_turn', 'tool_executions', ['conversation_id', 'turn_id'], unique=False)
    op.create_index(op.f('ix_tool_executions_execution_site_id'), 'tool_executions', ['execution_site_id'], unique=False)
    op.create_index(op.f('ix_tool_executions_runner_id'), 'tool_executions', ['runner_id'], unique=False)
    op.create_index(op.f('ix_tool_executions_runtime_job_id'), 'tool_executions', ['runtime_job_id'], unique=False)
    op.create_index('ix_tool_executions_status_created', 'tool_executions', ['status', 'created_at'], unique=False)
    op.create_index('ix_tool_executions_task_created', 'tool_executions', ['task_id', 'created_at'], unique=False)
    op.create_index('ix_tool_executions_task_tool_created', 'tool_executions', ['task_id', 'tool_name', 'created_at'], unique=False)
    op.create_index('ix_tool_executions_task_turn_seq', 'tool_executions', ['task_id', 'turn_sequence'], unique=False)
    op.create_index('ix_tool_executions_tenant_command', 'tool_executions', ['tenant_id', 'command_id'], unique=False)
    op.create_index(op.f('ix_tool_executions_tenant_id'), 'tool_executions', ['tenant_id'], unique=False)
    op.create_index('ix_tool_executions_tenant_runtime_job', 'tool_executions', ['tenant_id', 'runtime_job_id'], unique=False)
    op.create_index('ix_tool_executions_tenant_task_created', 'tool_executions', ['tenant_id', 'task_id', 'created_at'], unique=False)
    op.create_index(op.f('ix_tool_executions_workspace_id'), 'tool_executions', ['workspace_id'], unique=False)
    op.create_index('ux_tool_executions_task_tool_call_id', 'tool_executions', ['task_id', 'tool_call_id'], unique=True)
    op.create_table('engagement_finding_links',
    sa.Column('id', _guid_type(), nullable=False),
    sa.Column('tenant_id', sa.Integer(), nullable=False),
    sa.Column('engagement_id', sa.Integer(), nullable=False),
    sa.Column('finding_id', _guid_type(), nullable=False),
    sa.Column('first_seen_in_engagement', sa.DateTime(timezone=True), nullable=False),
    sa.Column('last_seen_in_engagement', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['engagement_id'], ['engagements.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['finding_id'], ['knowledge_findings.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('engagement_id', 'finding_id', name='ux_engagement_finding_links')
    )
    op.create_index('ix_engagement_finding_links_engagement', 'engagement_finding_links', ['engagement_id'], unique=False)
    op.create_index('ix_engagement_finding_links_finding', 'engagement_finding_links', ['finding_id'], unique=False)
    op.create_index('ix_engagement_finding_links_tenant_engagement', 'engagement_finding_links', ['tenant_id', 'engagement_id'], unique=False)
    op.create_index(op.f('ix_engagement_finding_links_tenant_id'), 'engagement_finding_links', ['tenant_id'], unique=False)
    op.create_table('engagement_web_path_links',
    sa.Column('id', _guid_type(), nullable=False),
    sa.Column('tenant_id', sa.Integer(), nullable=False),
    sa.Column('engagement_id', sa.Integer(), nullable=False),
    sa.Column('web_path_id', _guid_type(), nullable=False),
    sa.Column('first_seen_in_engagement', sa.DateTime(timezone=True), nullable=False),
    sa.Column('last_seen_in_engagement', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['engagement_id'], ['engagements.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['web_path_id'], ['knowledge_web_paths.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('engagement_id', 'web_path_id', name='ux_engagement_web_path_links')
    )
    op.create_index('ix_engagement_web_path_links_engagement', 'engagement_web_path_links', ['engagement_id'], unique=False)
    op.create_index('ix_engagement_web_path_links_tenant_engagement', 'engagement_web_path_links', ['tenant_id', 'engagement_id'], unique=False)
    op.create_index(op.f('ix_engagement_web_path_links_tenant_id'), 'engagement_web_path_links', ['tenant_id'], unique=False)
    op.create_index('ix_engagement_web_path_links_web_path', 'engagement_web_path_links', ['web_path_id'], unique=False)
    op.create_table('execution_artifacts',
    sa.Column('id', _guid_type(), nullable=False),
    sa.Column('execution_id', _guid_type(), nullable=False),
    sa.Column('manifest_id', _guid_type(), nullable=True),
    sa.Column('tenant_id', sa.Integer(), nullable=False),
    sa.Column('task_id', sa.Integer(), nullable=False),
    sa.Column('runtime_job_id', _guid_type(), nullable=True),
    sa.Column('runner_id', _guid_type(), nullable=True),
    sa.Column('command_id', sa.String(length=255), nullable=True),
    sa.Column('artifact_kind', sa.String(length=50), nullable=False),
    sa.Column('relative_path', sa.Text(), nullable=True),
    sa.Column('source_path', sa.Text(), nullable=True),
    sa.Column('fallback_path', sa.Text(), nullable=True),
    sa.Column('object_key', sa.Text(), nullable=True),
    sa.Column('storage_backend', sa.String(length=64), nullable=True),
    sa.Column('upload_status', sa.String(length=32), server_default='inline', nullable=False),
    sa.Column('uploaded_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('content_text', sa.Text(), nullable=True),
    sa.Column('content_sha256', sa.String(length=64), nullable=True),
    sa.Column('byte_size', sa.BigInteger(), nullable=True),
    sa.Column('mime_type', sa.String(length=255), nullable=True),
    sa.Column('is_text', sa.Boolean(), server_default=sa.text('true'), nullable=False),
    sa.Column('metadata', sa.JSON(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.ForeignKeyConstraint(['execution_id'], ['tool_executions.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['manifest_id'], ['artifact_manifests.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['runner_id'], ['runners.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['runtime_job_id'], ['runtime_jobs.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['task_id'], ['tasks.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_execution_artifacts_command_id'), 'execution_artifacts', ['command_id'], unique=False)
    op.create_index('ix_execution_artifacts_execution_kind', 'execution_artifacts', ['execution_id', 'artifact_kind'], unique=False)
    op.create_index(op.f('ix_execution_artifacts_manifest_id'), 'execution_artifacts', ['manifest_id'], unique=False)
    op.create_index(op.f('ix_execution_artifacts_runner_id'), 'execution_artifacts', ['runner_id'], unique=False)
    op.create_index(op.f('ix_execution_artifacts_runtime_job_id'), 'execution_artifacts', ['runtime_job_id'], unique=False)
    op.create_index('ix_execution_artifacts_task_created', 'execution_artifacts', ['task_id', 'created_at'], unique=False)
    op.create_index('ix_execution_artifacts_task_kind_created', 'execution_artifacts', ['task_id', 'artifact_kind', 'created_at'], unique=False)
    op.create_index('ix_execution_artifacts_tenant_command', 'execution_artifacts', ['tenant_id', 'command_id'], unique=False)
    op.create_index(op.f('ix_execution_artifacts_tenant_id'), 'execution_artifacts', ['tenant_id'], unique=False)
    op.create_index('ix_execution_artifacts_tenant_object_key', 'execution_artifacts', ['tenant_id', 'object_key'], unique=False)
    op.create_index('ix_execution_artifacts_tenant_runtime_job', 'execution_artifacts', ['tenant_id', 'runtime_job_id'], unique=False)
    op.create_index('ix_execution_artifacts_tenant_task_created', 'execution_artifacts', ['tenant_id', 'task_id', 'created_at'], unique=False)
    _create_tenant_isolation_rls()


def downgrade() -> None:
    # ### commands auto generated by Alembic - please adjust! ###
    op.drop_index('ix_execution_artifacts_tenant_task_created', table_name='execution_artifacts')
    op.drop_index('ix_execution_artifacts_tenant_runtime_job', table_name='execution_artifacts')
    op.drop_index('ix_execution_artifacts_tenant_object_key', table_name='execution_artifacts')
    op.drop_index(op.f('ix_execution_artifacts_tenant_id'), table_name='execution_artifacts')
    op.drop_index('ix_execution_artifacts_tenant_command', table_name='execution_artifacts')
    op.drop_index('ix_execution_artifacts_task_kind_created', table_name='execution_artifacts')
    op.drop_index('ix_execution_artifacts_task_created', table_name='execution_artifacts')
    op.drop_index(op.f('ix_execution_artifacts_runtime_job_id'), table_name='execution_artifacts')
    op.drop_index(op.f('ix_execution_artifacts_runner_id'), table_name='execution_artifacts')
    op.drop_index(op.f('ix_execution_artifacts_manifest_id'), table_name='execution_artifacts')
    op.drop_index('ix_execution_artifacts_execution_kind', table_name='execution_artifacts')
    op.drop_index(op.f('ix_execution_artifacts_command_id'), table_name='execution_artifacts')
    op.drop_table('execution_artifacts')
    op.drop_index('ix_engagement_web_path_links_web_path', table_name='engagement_web_path_links')
    op.drop_index(op.f('ix_engagement_web_path_links_tenant_id'), table_name='engagement_web_path_links')
    op.drop_index('ix_engagement_web_path_links_tenant_engagement', table_name='engagement_web_path_links')
    op.drop_index('ix_engagement_web_path_links_engagement', table_name='engagement_web_path_links')
    op.drop_table('engagement_web_path_links')
    op.drop_index(op.f('ix_engagement_finding_links_tenant_id'), table_name='engagement_finding_links')
    op.drop_index('ix_engagement_finding_links_tenant_engagement', table_name='engagement_finding_links')
    op.drop_index('ix_engagement_finding_links_finding', table_name='engagement_finding_links')
    op.drop_index('ix_engagement_finding_links_engagement', table_name='engagement_finding_links')
    op.drop_table('engagement_finding_links')
    op.drop_index('ux_tool_executions_task_tool_call_id', table_name='tool_executions')
    op.drop_index(op.f('ix_tool_executions_workspace_id'), table_name='tool_executions')
    op.drop_index('ix_tool_executions_tenant_task_created', table_name='tool_executions')
    op.drop_index('ix_tool_executions_tenant_runtime_job', table_name='tool_executions')
    op.drop_index(op.f('ix_tool_executions_tenant_id'), table_name='tool_executions')
    op.drop_index('ix_tool_executions_tenant_command', table_name='tool_executions')
    op.drop_index('ix_tool_executions_task_turn_seq', table_name='tool_executions')
    op.drop_index('ix_tool_executions_task_tool_created', table_name='tool_executions')
    op.drop_index('ix_tool_executions_task_created', table_name='tool_executions')
    op.drop_index('ix_tool_executions_status_created', table_name='tool_executions')
    op.drop_index(op.f('ix_tool_executions_runtime_job_id'), table_name='tool_executions')
    op.drop_index(op.f('ix_tool_executions_runner_id'), table_name='tool_executions')
    op.drop_index(op.f('ix_tool_executions_execution_site_id'), table_name='tool_executions')
    op.drop_index('ix_tool_executions_conversation_turn', table_name='tool_executions')
    op.drop_index(op.f('ix_tool_executions_command_id'), table_name='tool_executions')
    op.drop_index('ix_tool_executions_chat_message', table_name='tool_executions')
    op.drop_table('tool_executions')
    op.drop_index('ix_tool_calls_tenant_message_created', table_name='tool_calls')
    op.drop_index(op.f('ix_tool_calls_tenant_id'), table_name='tool_calls')
    op.drop_index('ix_tool_calls_parent', table_name='tool_calls')
    op.drop_index('ix_tool_calls_message', table_name='tool_calls')
    op.drop_index(op.f('ix_tool_calls_id'), table_name='tool_calls')
    op.drop_table('tool_calls')
    op.drop_index('uq_runner_control_messages_outbound_idempotency', table_name='runner_control_messages', postgresql_where=sa.text("direction = 'outbound' AND idempotency_key IS NOT NULL"), sqlite_where=sa.text("direction = 'outbound' AND idempotency_key IS NOT NULL"))
    op.drop_index('uq_runner_control_messages_inbound_idempotency', table_name='runner_control_messages', postgresql_where=sa.text("direction = 'inbound' AND idempotency_key IS NOT NULL"), sqlite_where=sa.text("direction = 'inbound' AND idempotency_key IS NOT NULL"))
    op.drop_index('ix_runner_control_messages_tenant_status', table_name='runner_control_messages')
    op.drop_index(op.f('ix_runner_control_messages_tenant_id'), table_name='runner_control_messages')
    op.drop_index(op.f('ix_runner_control_messages_task_id'), table_name='runner_control_messages')
    op.drop_index(op.f('ix_runner_control_messages_runtime_job_id'), table_name='runner_control_messages')
    op.drop_index('ix_runner_control_messages_runtime_job', table_name='runner_control_messages')
    op.drop_index(op.f('ix_runner_control_messages_runner_id'), table_name='runner_control_messages')
    op.drop_table('runner_control_messages')
    op.drop_index(op.f('ix_knowledge_web_paths_user_id'), table_name='knowledge_web_paths')
    op.drop_index('ix_knowledge_web_paths_tenant_user_url', table_name='knowledge_web_paths')
    op.drop_index('ix_knowledge_web_paths_tenant_user_service', table_name='knowledge_web_paths')
    op.drop_index('ix_knowledge_web_paths_tenant_user_origin', table_name='knowledge_web_paths')
    op.drop_index('ix_knowledge_web_paths_tenant_user_last_seen', table_name='knowledge_web_paths')
    op.drop_index('ix_knowledge_web_paths_tenant_user_asset', table_name='knowledge_web_paths')
    op.drop_index(op.f('ix_knowledge_web_paths_tenant_id'), table_name='knowledge_web_paths')
    op.drop_table('knowledge_web_paths')
    op.drop_index(op.f('ix_knowledge_findings_user_id'), table_name='knowledge_findings')
    op.drop_index('ix_knowledge_findings_tenant_user_status', table_name='knowledge_findings')
    op.drop_index('ix_knowledge_findings_tenant_user_service', table_name='knowledge_findings')
    op.drop_index('ix_knowledge_findings_tenant_user_finding_key', table_name='knowledge_findings')
    op.drop_index('ix_knowledge_findings_tenant_user_asset', table_name='knowledge_findings')
    op.drop_index(op.f('ix_knowledge_findings_tenant_id'), table_name='knowledge_findings')
    op.drop_table('knowledge_findings')
    op.drop_index(op.f('ix_engagement_service_links_tenant_id'), table_name='engagement_service_links')
    op.drop_index('ix_engagement_service_links_tenant_engagement', table_name='engagement_service_links')
    op.drop_index('ix_engagement_service_links_service', table_name='engagement_service_links')
    op.drop_index('ix_engagement_service_links_engagement', table_name='engagement_service_links')
    op.drop_table('engagement_service_links')
    op.drop_index('ix_chat_turn_events_tenant_task_created', table_name='chat_turn_events')
    op.drop_index(op.f('ix_chat_turn_events_tenant_id'), table_name='chat_turn_events')
    op.drop_index('ix_chat_turn_events_task_conv_turn_phase', table_name='chat_turn_events')
    op.drop_index(op.f('ix_chat_turn_events_id'), table_name='chat_turn_events')
    op.drop_table('chat_turn_events')
    op.drop_index('ux_artifact_manifests_tenant_runtime_command_workspace_message', table_name='artifact_manifests')
    op.drop_index('ix_artifact_manifests_tenant_task_created', table_name='artifact_manifests')
    op.drop_index('ix_artifact_manifests_tenant_runtime_job', table_name='artifact_manifests')
    op.drop_index('ix_artifact_manifests_tenant_idempotency', table_name='artifact_manifests')
    op.drop_index(op.f('ix_artifact_manifests_tenant_id'), table_name='artifact_manifests')
    op.drop_index(op.f('ix_artifact_manifests_task_id'), table_name='artifact_manifests')
    op.drop_index(op.f('ix_artifact_manifests_runtime_job_id'), table_name='artifact_manifests')
    op.drop_index(op.f('ix_artifact_manifests_runner_id'), table_name='artifact_manifests')
    op.drop_table('artifact_manifests')
    op.drop_index('ix_turn_workflows_tenant_task_turn_sequence', table_name='turn_workflows')
    op.drop_index(op.f('ix_turn_workflows_tenant_id'), table_name='turn_workflows')
    op.drop_index('ix_turn_workflows_task_turn_sequence', table_name='turn_workflows')
    op.drop_index('ix_turn_workflows_task_state', table_name='turn_workflows')
    op.drop_index('ix_turn_workflows_task_resume_key', table_name='turn_workflows')
    op.drop_index('ix_turn_workflows_task_checkpoint', table_name='turn_workflows')
    op.drop_index(op.f('ix_turn_workflows_id'), table_name='turn_workflows')
    op.drop_table('turn_workflows')
    op.drop_index('ix_task_history_tenant_task_timestamp', table_name='task_history')
    op.drop_index(op.f('ix_task_history_tenant_id'), table_name='task_history')
    op.drop_index(op.f('ix_task_history_id'), table_name='task_history')
    op.drop_table('task_history')
    op.drop_index('ux_task_closure_memos_version', table_name='task_closure_memos')
    op.drop_index('ux_task_closure_memos_preparing', table_name='task_closure_memos', sqlite_where=sa.text("status = 'preparing'"), postgresql_where=sa.text("status = 'preparing'"))
    op.drop_index('ux_task_closure_memos_current_ready', table_name='task_closure_memos', sqlite_where=sa.text("status = 'ready' AND is_current IS true"), postgresql_where=sa.text("status = 'ready' AND is_current IS true"))
    op.drop_index(op.f('ix_task_closure_memos_user_id'), table_name='task_closure_memos')
    op.drop_index('ix_task_closure_memos_tenant_user_updated', table_name='task_closure_memos')
    op.drop_index('ix_task_closure_memos_tenant_user_engagement_task_current', table_name='task_closure_memos')
    op.drop_index(op.f('ix_task_closure_memos_tenant_id'), table_name='task_closure_memos')
    op.drop_index('ix_task_closure_memos_tenant_engagement_task', table_name='task_closure_memos')
    op.drop_index('ix_task_closure_memos_tenant_engagement_status', table_name='task_closure_memos')
    op.drop_table('task_closure_memos')
    op.drop_index('ix_system_logs_timestamp', table_name='system_logs')
    op.drop_index('ix_system_logs_tenant_task_sequence', table_name='system_logs')
    op.drop_index(op.f('ix_system_logs_tenant_id'), table_name='system_logs')
    op.drop_index('ix_system_logs_task_sequence', table_name='system_logs')
    op.drop_index(op.f('ix_system_logs_id'), table_name='system_logs')
    op.drop_table('system_logs')
    op.drop_index('ix_stream_events_timestamp', table_name='stream_events')
    op.drop_index('ix_stream_events_tenant_task_sequence', table_name='stream_events')
    op.drop_index(op.f('ix_stream_events_tenant_id'), table_name='stream_events')
    op.drop_index('ix_stream_events_task_turn', table_name='stream_events')
    op.drop_index('ix_stream_events_task_sequence', table_name='stream_events')
    op.drop_index('ix_stream_events_task_conversation', table_name='stream_events')
    op.drop_index(op.f('ix_stream_events_id'), table_name='stream_events')
    op.drop_table('stream_events')
    op.drop_index('ix_runtime_jobs_tenant_status', table_name='runtime_jobs')
    op.drop_index('ix_runtime_jobs_tenant_runner_status', table_name='runtime_jobs')
    op.drop_index(op.f('ix_runtime_jobs_tenant_id'), table_name='runtime_jobs')
    op.drop_index(op.f('ix_runtime_jobs_task_id'), table_name='runtime_jobs')
    op.drop_index(op.f('ix_runtime_jobs_runner_id'), table_name='runtime_jobs')
    op.drop_index(op.f('ix_runtime_jobs_execution_site_id'), table_name='runtime_jobs')
    op.drop_table('runtime_jobs')
    op.drop_index('ix_runner_credentials_tenant_runner', table_name='runner_credentials')
    op.drop_index(op.f('ix_runner_credentials_tenant_id'), table_name='runner_credentials')
    op.drop_index(op.f('ix_runner_credentials_runner_id'), table_name='runner_credentials')
    op.drop_table('runner_credentials')
    op.drop_index('ix_runner_connections_tenant_status', table_name='runner_connections')
    op.drop_index('ix_runner_connections_tenant_runner_last_seen', table_name='runner_connections')
    op.drop_index(op.f('ix_runner_connections_tenant_id'), table_name='runner_connections')
    op.drop_index(op.f('ix_runner_connections_runner_id'), table_name='runner_connections')
    op.drop_table('runner_connections')
    op.drop_index('ix_reports_tenant_task_created', table_name='reports')
    op.drop_index(op.f('ix_reports_tenant_id'), table_name='reports')
    op.drop_index(op.f('ix_reports_id'), table_name='reports')
    op.drop_table('reports')
    op.drop_index('ix_llm_usage_user_created', table_name='llm_usage_records')
    op.drop_index('ix_llm_usage_tenant_task_created', table_name='llm_usage_records')
    op.drop_index('ix_llm_usage_task_model', table_name='llm_usage_records')
    op.drop_index('ix_llm_usage_task_created', table_name='llm_usage_records')
    op.drop_index(op.f('ix_llm_usage_records_user_id'), table_name='llm_usage_records')
    op.drop_index(op.f('ix_llm_usage_records_tenant_id'), table_name='llm_usage_records')
    op.drop_index(op.f('ix_llm_usage_records_task_id'), table_name='llm_usage_records')
    op.drop_index(op.f('ix_llm_usage_records_id'), table_name='llm_usage_records')
    op.drop_table('llm_usage_records')
    op.drop_index(op.f('ix_llm_conversations_user_id'), table_name='llm_conversations')
    op.drop_index('ix_llm_conversations_tenant_task_created', table_name='llm_conversations')
    op.drop_index(op.f('ix_llm_conversations_tenant_id'), table_name='llm_conversations')
    op.drop_index('ix_llm_conversations_task_user_provider', table_name='llm_conversations')
    op.drop_index(op.f('ix_llm_conversations_task_id'), table_name='llm_conversations')
    op.drop_index(op.f('ix_llm_conversations_id'), table_name='llm_conversations')
    op.drop_table('llm_conversations')
    op.drop_index(op.f('ix_knowledge_services_user_id'), table_name='knowledge_services')
    op.drop_index('ix_knowledge_services_tenant_user_service_key', table_name='knowledge_services')
    op.drop_index('ix_knowledge_services_tenant_user_last_seen', table_name='knowledge_services')
    op.drop_index('ix_knowledge_services_tenant_user_asset', table_name='knowledge_services')
    op.drop_index(op.f('ix_knowledge_services_tenant_id'), table_name='knowledge_services')
    op.drop_table('knowledge_services')
    op.drop_index(op.f('ix_knowledge_observations_user_id'), table_name='knowledge_observations')
    op.drop_index('ix_knowledge_observations_user_created', table_name='knowledge_observations')
    op.drop_index('ix_knowledge_observations_tenant_source_execution', table_name='knowledge_observations')
    op.drop_index(op.f('ix_knowledge_observations_tenant_id'), table_name='knowledge_observations')
    op.drop_index('ix_knowledge_observations_tenant_created', table_name='knowledge_observations')
    op.drop_index('ix_knowledge_observations_source_execution', table_name='knowledge_observations')
    op.drop_index('ix_knowledge_observations_engagement_subject', table_name='knowledge_observations')
    op.drop_index('ix_knowledge_observations_engagement_created', table_name='knowledge_observations')
    op.drop_table('knowledge_observations')
    op.drop_index('ux_interrupt_tickets_task_pending', table_name='interrupt_tickets', postgresql_where=sa.text("state = 'PENDING'"), sqlite_where=sa.text("state = 'PENDING'"))
    op.drop_index('ix_interrupt_tickets_tool_call_id', table_name='interrupt_tickets')
    op.drop_index('ix_interrupt_tickets_tenant_task_state', table_name='interrupt_tickets')
    op.drop_index(op.f('ix_interrupt_tickets_tenant_id'), table_name='interrupt_tickets')
    op.drop_index('ix_interrupt_tickets_task_turn_sequence', table_name='interrupt_tickets')
    op.drop_index('ix_interrupt_tickets_task_state', table_name='interrupt_tickets')
    op.drop_index('ix_interrupt_tickets_interrupt_id', table_name='interrupt_tickets')
    op.drop_index(op.f('ix_interrupt_tickets_id'), table_name='interrupt_tickets')
    op.drop_table('interrupt_tickets')
    op.drop_index(op.f('ix_engagement_asset_links_tenant_id'), table_name='engagement_asset_links')
    op.drop_index('ix_engagement_asset_links_tenant_engagement', table_name='engagement_asset_links')
    op.drop_index('ix_engagement_asset_links_engagement', table_name='engagement_asset_links')
    op.drop_index('ix_engagement_asset_links_asset', table_name='engagement_asset_links')
    op.drop_table('engagement_asset_links')
    op.drop_index('ix_chat_messages_tenant_task_created', table_name='chat_messages')
    op.drop_index(op.f('ix_chat_messages_tenant_id'), table_name='chat_messages')
    op.drop_index('ix_chat_messages_task_turn', table_name='chat_messages')
    op.drop_index('ix_chat_messages_task_conversation', table_name='chat_messages')
    op.drop_index('ix_chat_messages_parent', table_name='chat_messages')
    op.drop_index(op.f('ix_chat_messages_id'), table_name='chat_messages')
    op.drop_index('ix_chat_messages_created', table_name='chat_messages')
    op.drop_table('chat_messages')
    op.drop_index('ix_agent_logs_turn_id', table_name='agent_logs')
    op.drop_index('ix_agent_logs_tenant_task_sequence', table_name='agent_logs')
    op.drop_index(op.f('ix_agent_logs_tenant_id'), table_name='agent_logs')
    op.drop_index('ix_agent_logs_task_turn', table_name='agent_logs')
    op.drop_index('ix_agent_logs_task_timestamp', table_name='agent_logs')
    op.drop_index('ix_agent_logs_task_sequence', table_name='agent_logs')
    op.drop_index('ix_agent_logs_task_conversation', table_name='agent_logs')
    op.drop_index('ix_agent_logs_sequence_timestamp', table_name='agent_logs')
    op.drop_index(op.f('ix_agent_logs_id'), table_name='agent_logs')
    op.drop_index('ix_agent_logs_conversation_id', table_name='agent_logs')
    op.drop_table('agent_logs')
    op.drop_index(op.f('ix_tasks_tenant_id'), table_name='tasks')
    op.drop_index(op.f('ix_tasks_id'), table_name='tasks')
    op.drop_index(op.f('ix_tasks_engagement_id'), table_name='tasks')
    op.drop_table('tasks')
    op.drop_index('ix_semantic_memories_user_tier', table_name='semantic_memories')
    op.drop_index(op.f('ix_semantic_memories_user_id'), table_name='semantic_memories')
    op.drop_index('ix_semantic_memories_user_engagement', table_name='semantic_memories', postgresql_where=sa.text('engagement_id IS NOT NULL'))
    op.drop_index('ix_semantic_memories_user_created', table_name='semantic_memories')
    op.drop_index('ix_semantic_memories_tenant_scope', table_name='semantic_memories', postgresql_where=sa.text('tenant_id IS NOT NULL'))
    op.drop_index(op.f('ix_semantic_memories_tenant_id'), table_name='semantic_memories')
    op.drop_index('ix_semantic_memories_embedding_identity', table_name='semantic_memories')
    op.drop_table('semantic_memories')
    op.drop_index('ix_runners_tenant_status', table_name='runners')
    op.drop_index('ix_runners_tenant_last_seen', table_name='runners')
    op.drop_index(op.f('ix_runners_tenant_id'), table_name='runners')
    op.drop_index(op.f('ix_runners_execution_site_id'), table_name='runners')
    op.drop_table('runners')
    op.drop_index('ix_runner_install_tokens_tenant_site_status', table_name='runner_install_tokens')
    op.drop_index(op.f('ix_runner_install_tokens_tenant_id'), table_name='runner_install_tokens')
    op.drop_index(op.f('ix_runner_install_tokens_execution_site_id'), table_name='runner_install_tokens')
    op.drop_index(op.f('ix_runner_install_tokens_created_by_user_id'), table_name='runner_install_tokens')
    op.drop_table('runner_install_tokens')
    op.drop_index(op.f('ix_knowledge_relationships_user_id'), table_name='knowledge_relationships')
    op.drop_index('ix_knowledge_relationships_tenant_user_type', table_name='knowledge_relationships')
    op.drop_index('ix_knowledge_relationships_tenant_user_target', table_name='knowledge_relationships')
    op.drop_index('ix_knowledge_relationships_tenant_user_source', table_name='knowledge_relationships')
    op.drop_index('ix_knowledge_relationships_tenant_user_relationship_key', table_name='knowledge_relationships')
    op.drop_index(op.f('ix_knowledge_relationships_tenant_id'), table_name='knowledge_relationships')
    op.drop_table('knowledge_relationships')
    op.drop_index('ux_knowledge_runs_engagement_exec_extractor', table_name='knowledge_ingestion_runs')
    op.drop_index('ix_knowledge_runs_user_created', table_name='knowledge_ingestion_runs')
    op.drop_index('ix_knowledge_runs_tenant_source_execution', table_name='knowledge_ingestion_runs')
    op.drop_index('ix_knowledge_runs_tenant_created', table_name='knowledge_ingestion_runs')
    op.drop_index('ix_knowledge_runs_source_execution', table_name='knowledge_ingestion_runs')
    op.drop_index('ix_knowledge_runs_engagement_created', table_name='knowledge_ingestion_runs')
    op.drop_index(op.f('ix_knowledge_ingestion_runs_user_id'), table_name='knowledge_ingestion_runs')
    op.drop_index(op.f('ix_knowledge_ingestion_runs_tenant_id'), table_name='knowledge_ingestion_runs')
    op.drop_table('knowledge_ingestion_runs')
    op.drop_index(op.f('ix_knowledge_evidence_archives_user_id'), table_name='knowledge_evidence_archives')
    op.drop_index(op.f('ix_knowledge_evidence_archives_tenant_id'), table_name='knowledge_evidence_archives')
    op.drop_index('ix_knowledge_archives_user_created', table_name='knowledge_evidence_archives')
    op.drop_index('ix_knowledge_archives_tenant_user_engagement_created', table_name='knowledge_evidence_archives')
    op.drop_index('ix_knowledge_archives_tenant_object_key', table_name='knowledge_evidence_archives')
    op.drop_index('ix_knowledge_archives_tenant_created', table_name='knowledge_evidence_archives')
    op.drop_index('ix_knowledge_archives_source_execution', table_name='knowledge_evidence_archives')
    op.drop_index('ix_knowledge_archives_source_artifact', table_name='knowledge_evidence_archives')
    op.drop_index('ix_knowledge_archives_engagement_created', table_name='knowledge_evidence_archives')
    op.drop_table('knowledge_evidence_archives')
    op.drop_index(op.f('ix_knowledge_assets_user_id'), table_name='knowledge_assets')
    op.drop_index('ix_knowledge_assets_tenant_user_last_seen', table_name='knowledge_assets')
    op.drop_index('ix_knowledge_assets_tenant_user_asset_type', table_name='knowledge_assets')
    op.drop_index('ix_knowledge_assets_tenant_user_asset_key', table_name='knowledge_assets')
    op.drop_index(op.f('ix_knowledge_assets_tenant_id'), table_name='knowledge_assets')
    op.drop_table('knowledge_assets')
    op.drop_index('ux_engagement_report_jobs_tenant_idempotency', table_name='engagement_report_jobs', sqlite_where=sa.text('idempotency_key IS NOT NULL'), postgresql_where=sa.text('idempotency_key IS NOT NULL'))
    op.drop_index(op.f('ix_engagement_report_jobs_user_id'), table_name='engagement_report_jobs')
    op.drop_index('ix_engagement_report_jobs_tenant_user_status', table_name='engagement_report_jobs')
    op.drop_index('ix_engagement_report_jobs_tenant_status_created', table_name='engagement_report_jobs')
    op.drop_index(op.f('ix_engagement_report_jobs_tenant_id'), table_name='engagement_report_jobs')
    op.drop_index('ix_engagement_report_jobs_tenant_engagement_created', table_name='engagement_report_jobs')
    op.drop_index('ix_engagement_report_jobs_status_created', table_name='engagement_report_jobs')
    op.drop_index('ix_engagement_report_jobs_locked_at', table_name='engagement_report_jobs')
    op.drop_table('engagement_report_jobs')
    op.drop_index(op.f('ix_user_settings_id'), table_name='user_settings')
    op.drop_table('user_settings')
    op.drop_index(op.f('ix_user_sessions_user_id'), table_name='user_sessions')
    op.drop_index(op.f('ix_user_sessions_revoked_at'), table_name='user_sessions')
    op.drop_index(op.f('ix_user_sessions_refresh_token_hash'), table_name='user_sessions')
    op.drop_index(op.f('ix_user_sessions_idle_expires_at'), table_name='user_sessions')
    op.drop_index(op.f('ix_user_sessions_id'), table_name='user_sessions')
    op.drop_index(op.f('ix_user_sessions_absolute_expires_at'), table_name='user_sessions')
    op.drop_table('user_sessions')
    op.drop_index(op.f('ix_user_reporting_llm_selections_user_id'), table_name='user_reporting_llm_selections')
    op.drop_index('ix_user_reporting_llm_selections_provider_model', table_name='user_reporting_llm_selections')
    op.drop_index(op.f('ix_user_reporting_llm_selections_id'), table_name='user_reporting_llm_selections')
    op.drop_table('user_reporting_llm_selections')
    op.drop_index(op.f('ix_user_memory_llm_selections_user_id'), table_name='user_memory_llm_selections')
    op.drop_index('ix_user_memory_llm_selections_provider_models', table_name='user_memory_llm_selections')
    op.drop_index(op.f('ix_user_memory_llm_selections_id'), table_name='user_memory_llm_selections')
    op.drop_table('user_memory_llm_selections')
    op.drop_index(op.f('ix_user_llm_selections_user_id'), table_name='user_llm_selections')
    op.drop_index('ix_user_llm_selections_provider_model', table_name='user_llm_selections')
    op.drop_index(op.f('ix_user_llm_selections_id'), table_name='user_llm_selections')
    op.drop_table('user_llm_selections')
    op.drop_index(op.f('ix_user_llm_provider_credentials_user_id'), table_name='user_llm_provider_credentials')
    op.drop_index('ix_user_llm_provider_credentials_provider', table_name='user_llm_provider_credentials')
    op.drop_index(op.f('ix_user_llm_provider_credentials_id'), table_name='user_llm_provider_credentials')
    op.drop_table('user_llm_provider_credentials')
    op.drop_index(op.f('ix_user_embedding_selections_user_id'), table_name='user_embedding_selections')
    op.drop_index('ix_user_embedding_selections_provider_model', table_name='user_embedding_selections')
    op.drop_index(op.f('ix_user_embedding_selections_id'), table_name='user_embedding_selections')
    op.drop_table('user_embedding_selections')
    op.drop_index(op.f('ix_tenant_memberships_user_id'), table_name='tenant_memberships')
    op.drop_index(op.f('ix_tenant_memberships_tenant_id'), table_name='tenant_memberships')
    op.drop_index(op.f('ix_tenant_memberships_id'), table_name='tenant_memberships')
    op.drop_table('tenant_memberships')
    op.drop_index('ix_tenant_data_management_settings_tenant_id', table_name='tenant_data_management_settings')
    op.drop_index(op.f('ix_tenant_data_management_settings_id'), table_name='tenant_data_management_settings')
    op.drop_table('tenant_data_management_settings')
    op.drop_index('ix_provenance_user_entity', table_name='knowledge_entity_provenance')
    op.drop_index('ix_provenance_tenant_entity', table_name='knowledge_entity_provenance')
    op.drop_index('ix_provenance_task', table_name='knowledge_entity_provenance')
    op.drop_index('ix_provenance_execution', table_name='knowledge_entity_provenance')
    op.drop_index(op.f('ix_knowledge_entity_provenance_user_id'), table_name='knowledge_entity_provenance')
    op.drop_index(op.f('ix_knowledge_entity_provenance_tenant_id'), table_name='knowledge_entity_provenance')
    op.drop_table('knowledge_entity_provenance')
    op.drop_index('ix_execution_sites_tenant_status', table_name='execution_sites')
    op.drop_index(op.f('ix_execution_sites_tenant_id'), table_name='execution_sites')
    op.drop_table('execution_sites')
    op.drop_index(op.f('ix_engagements_user_id'), table_name='engagements')
    op.drop_index(op.f('ix_engagements_tenant_id'), table_name='engagements')
    op.drop_index(op.f('ix_engagements_id'), table_name='engagements')
    op.drop_table('engagements')
    op.drop_index('ux_engagement_reports_version', table_name='engagement_reports')
    op.drop_index('ux_engagement_reports_current_ready', table_name='engagement_reports', sqlite_where=sa.text("status = 'ready' AND is_current IS true"), postgresql_where=sa.text("status = 'ready' AND is_current IS true"))
    op.drop_index(op.f('ix_engagement_reports_user_id'), table_name='engagement_reports')
    op.drop_index('ix_engagement_reports_tenant_user_engagement_type_current', table_name='engagement_reports')
    op.drop_index('ix_engagement_reports_tenant_user_created', table_name='engagement_reports')
    op.drop_index('ix_engagement_reports_tenant_status', table_name='engagement_reports')
    op.drop_index(op.f('ix_engagement_reports_tenant_id'), table_name='engagement_reports')
    op.drop_index('ix_engagement_reports_tenant_engagement_created', table_name='engagement_reports')
    op.drop_index('ix_engagement_reports_tenant_deletion_finalized', table_name='engagement_reports')
    op.drop_index('ix_engagement_reports_tenant_delete_undo', table_name='engagement_reports')
    op.drop_table('engagement_reports')
    op.drop_index('ix_cve_index_state_lease_expires_at', table_name='cve_index_state')
    op.drop_index('ix_cve_index_state_last_sync_status', table_name='cve_index_state')
    op.drop_index('ix_cve_index_state_last_attempt_started_at', table_name='cve_index_state')
    op.drop_index('ix_cve_index_state_last_attempt_finished_at', table_name='cve_index_state')
    op.drop_index(op.f('ix_cve_index_state_id'), table_name='cve_index_state')
    op.drop_table('cve_index_state')
    op.drop_index('ix_cve_affected_products_vendor_product_norm', table_name='cve_affected_products')
    op.drop_index('ix_cve_affected_products_vendor_norm', table_name='cve_affected_products')
    op.drop_index('ix_cve_affected_products_product_norm', table_name='cve_affected_products')
    op.drop_index(op.f('ix_cve_affected_products_id'), table_name='cve_affected_products')
    op.drop_index('ix_cve_affected_products_cve_record_id', table_name='cve_affected_products')
    op.drop_index('ix_cve_affected_products_cve_id', table_name='cve_affected_products')
    op.drop_table('cve_affected_products')
    op.drop_index(op.f('ix_users_username'), table_name='users')
    op.drop_index(op.f('ix_users_id'), table_name='users')
    op.drop_index(op.f('ix_users_email'), table_name='users')
    op.drop_table('users')
    op.drop_index(op.f('ix_tenants_slug'), table_name='tenants')
    op.drop_index(op.f('ix_tenants_id'), table_name='tenants')
    op.drop_table('tenants')
    op.drop_table('task_turn_counter')
    op.drop_table('platform_installations')
    op.drop_index('ix_cve_records_source_updated_at', table_name='cve_records')
    op.drop_index('ix_cve_records_record_state', table_name='cve_records')
    op.drop_index('ix_cve_records_projection_status', table_name='cve_records')
    op.drop_index(op.f('ix_cve_records_id'), table_name='cve_records')
    op.drop_index('ix_cve_records_cve_id', table_name='cve_records')
    op.drop_table('cve_records')
    op.drop_index('ix_cve_index_sync_runs_status', table_name='cve_index_sync_runs')
    op.drop_index('ix_cve_index_sync_runs_started_at', table_name='cve_index_sync_runs')
    op.drop_index(op.f('ix_cve_index_sync_runs_id'), table_name='cve_index_sync_runs')
    op.drop_index('ix_cve_index_sync_runs_finished_at', table_name='cve_index_sync_runs')
    op.drop_table('cve_index_sync_runs')
    op.drop_index(op.f('ix_cve_index_settings_id'), table_name='cve_index_settings')
    op.drop_table('cve_index_settings')
    # ### end Alembic commands ###
