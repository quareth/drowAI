/**
 * React Query hook for tenant data management settings.
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { toApiError } from "@/lib/api-config";
import { apiRequest } from "@/lib/queryClient";

export interface DataManagementSettingsResponse {
  tenant_id: number;
  report_retention_enabled: boolean;
  report_history_retention_days: number;
  created_at: string;
  updated_at: string;
}

export interface DataManagementSettingsUpdateRequest {
  report_retention_enabled?: boolean;
  report_history_retention_days?: number;
}

export const DATA_MANAGEMENT_SETTINGS_QUERY_KEY = [
  "/api/settings/data-management",
] as const;

async function putDataManagementSettings(
  payload: DataManagementSettingsUpdateRequest,
): Promise<DataManagementSettingsResponse> {
  const response = await apiRequest("PUT", "/api/settings/data-management", payload);
  if (!(response instanceof Response)) {
    throw new Error("Unexpected response while updating data management settings.");
  }
  if (!response.ok) {
    throw await toApiError(response, "Failed to update data management settings.");
  }
  return response.json() as Promise<DataManagementSettingsResponse>;
}

export function useDataManagementSettings(options: { enabled: boolean }) {
  const queryClient = useQueryClient();
  const settingsQuery = useQuery<DataManagementSettingsResponse>({
    queryKey: DATA_MANAGEMENT_SETTINGS_QUERY_KEY,
    enabled: options.enabled,
  });

  const updateMutation = useMutation({
    mutationFn: putDataManagementSettings,
    onSuccess: async () => {
      await queryClient.invalidateQueries({
        queryKey: DATA_MANAGEMENT_SETTINGS_QUERY_KEY,
      });
    },
  });

  return {
    settingsQuery,
    updateSettings: updateMutation.mutateAsync,
    updateSettingsMutation: updateMutation,
  };
}
