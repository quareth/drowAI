/**
 * Hook for account-level API settings data and mutations.
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { SESSION_EXPIRED_MESSAGE } from "@/components/settings/lib/settings-mutation-helpers";
import { toApiError } from "@/lib/api-config";
import { apiRequest } from "@/lib/queryClient";

export interface UserSettings {
  id: number;
  user_id: number;
  openai_api_key: string | null;
  openai_model: string;
  enable_ai: boolean;
  session_timeout: number;
  theme: string;
  timezone: string;
  created_at: string;
  updated_at: string;
}

export const API_SETTINGS_QUERY_KEY = ["/api/settings/"] as const;

interface TestOpenAiResponse {
  model_count: number;
}

type UseApiSettingsOptions = {
  enabled: boolean;
};

export function useApiSettings(options: UseApiSettingsOptions) {
  const queryClient = useQueryClient();

  const settingsQuery = useQuery<UserSettings>({
    queryKey: API_SETTINGS_QUERY_KEY,
    enabled: options.enabled,
  });

  const updateSettingsMutation = useMutation({
    mutationFn: async (data: Partial<UserSettings>) => {
      const response = await apiRequest("PUT", "/api/settings/", data);
      if (!(response instanceof Response)) {
        throw new Error("Unexpected response while updating settings.");
      }

      if (!response.ok) {
        if (response.status === 401) {
          throw new Error(SESSION_EXPIRED_MESSAGE);
        }
        throw await toApiError(response, "Failed to update settings");
      }
      return response.json() as Promise<UserSettings>;
    },
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: API_SETTINGS_QUERY_KEY });
    },
  });

  const testOpenAiMutation = useMutation({
    mutationFn: async (openAiApiKey: string | null) => {
      const response = await apiRequest("POST", "/api/settings/test-openai", {
        openai_api_key: openAiApiKey,
      });
      if (!(response instanceof Response)) {
        throw new Error("Unexpected response while testing OpenAI API.");
      }

      if (!response.ok) {
        if (response.status === 401) {
          throw new Error(SESSION_EXPIRED_MESSAGE);
        }
        throw await toApiError(response, "Failed to test OpenAI API");
      }
      return response.json() as Promise<TestOpenAiResponse>;
    },
  });

  return {
    settingsQuery,
    updateSettings: updateSettingsMutation.mutateAsync,
    updateSettingsMutation,
    testOpenAi: testOpenAiMutation.mutateAsync,
    testOpenAiMutation,
  };
}
