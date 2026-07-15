/**
 * Tenant-permission helpers derived from server-provided effective permissions.
 *
 * Responsibilities:
 * - normalize `effective_permissions.actions` into a stable lookup set
 * - provide fail-closed action checks for role-aware UI state
 */

export const TENANT_ACTIONS = {
  tenantMembershipManage: "tenant.membership.manage",
  tenantSettingsManage: "tenant.settings.manage",
  taskCreate: "task.create",
  taskControl: "task.control",
  taskDelete: "task.delete",
  knowledgeWrite: "knowledge.write",
  reportWrite: "report.write",
  reportDelete: "report.delete",
} as const;

export interface EffectivePermissionsLike {
  actions?: readonly string[] | null;
}

export function toTenantActionSet(
  effectivePermissions: EffectivePermissionsLike | null | undefined,
): ReadonlySet<string> {
  if (!effectivePermissions || !Array.isArray(effectivePermissions.actions)) {
    return new Set<string>();
  }
  return new Set(
    effectivePermissions.actions
      .map((action) => String(action).trim())
      .filter((action) => action.length > 0),
  );
}

export function hasTenantAction(
  actions: ReadonlySet<string>,
  action: string,
): boolean {
  return actions.has(action);
}
