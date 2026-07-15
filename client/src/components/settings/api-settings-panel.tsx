/**
 * API settings panel for provider-neutral LLM configuration.
 */
import { useToast } from "@/hooks/use-toast";
import { SESSION_EXPIRED_MESSAGE, showSessionExpiredToast } from "@/components/settings/lib/settings-mutation-helpers";
import ProviderSettingsSection from "@/features/llm-provider/ProviderSettingsSection";

type ApiSettingsPanelProps = {
  queryEnabled: boolean;
};

export function ApiSettingsPanel({ queryEnabled }: ApiSettingsPanelProps) {
  const { toast } = useToast();

  const onMutationError = (error: Error, fallbackTitle: string, fallbackDescription: string) => {
    if (error.message === SESSION_EXPIRED_MESSAGE) {
      showSessionExpiredToast(toast);
      return;
    }
    toast({
      title: fallbackTitle,
      description: error.message || fallbackDescription,
      variant: "destructive",
    });
  };

  return (
    <div className="space-y-6">
      <ProviderSettingsSection
        queryEnabled={queryEnabled}
        onSuccess={(title, description) => {
          toast({ title, description });
        }}
        onError={(title, error) => {
          onMutationError(error, title, "Failed to update provider settings.");
        }}
      />
    </div>
  );
}
