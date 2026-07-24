/**
 * Metadata-driven managed connection controls for one approved LLM preset.
 *
 * The panel renders only backend-declared user configuration fields and never
 * accepts headers or arbitrary provider inventory values.
 */
import { useState } from "react";
import { Key, Loader2, Trash2 } from "lucide-react";

import type {
  LLMCatalogModel,
  LLMConnectionMetadata,
} from "@/features/llm-provider/types";
import {
  ProviderApiKeyField,
  ProviderSettingsCard,
} from "@/features/llm-provider/ProviderSettingsCard";
import { useConnectionSettingsController } from "@/features/llm-provider/useConnectionSettingsController";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";

export interface ConnectionSettingsPanelProps {
  providerLabel: string;
  model: LLMCatalogModel;
  connection: LLMConnectionMetadata;
  hasStoredCredential: boolean;
  setupNote?: string | null;
  onSuccess: (title: string, description: string) => void;
  onError: (title: string, error: Error) => void;
}

export function ConnectionSettingsPanel({
  providerLabel,
  model,
  connection,
  hasStoredCredential,
  setupNote = null,
  onSuccess,
  onError,
}: ConnectionSettingsPanelProps) {
  const [fieldValues, setFieldValues] = useState<Record<string, string>>({});
  const configFields = connection.configFields?.length
    ? connection.configFields
    : connection.userConfigFields.map((name) => ({
        name,
        label: fallbackFieldLabel(name),
        fieldType: name === "api_key" ? "password" : "text",
        required: name === "api_key",
        secret: name === "api_key",
      }));
  const visibleConfigFields = configFields
    .filter((field) => field.name !== "display_label")
    .map((field) => {
      if (field.name === "wire_model_id") {
        return { ...field, label: "Model name" };
      }
      return field;
    });

  const requiredFieldsComplete = visibleConfigFields.every((field) =>
    !field.required || Boolean((fieldValues[field.name] ?? "").trim()),
  );
  const {
    connected,
    isPending,
    connect,
    disconnect,
  } = useConnectionSettingsController({
    providerLabel,
    model,
    connection,
    fieldValues,
    hasStoredCredential,
    onSuccess,
    onError,
  });

  return (
    <ProviderSettingsCard
      title={providerLabel}
      setupNote={setupNote}
      statusLabel={connected ? "Connected" : "Not connected"}
      statusPositive={connected}
    >
      {visibleConfigFields.map((field) => (
        field.name === "api_key" ? (
          <ProviderApiKeyField
            key={field.name}
            id={`llm-connection-${field.name}-${connection.presetId}`}
            value={fieldValues[field.name] ?? ""}
            onChange={(value) =>
              setFieldValues((current) => ({ ...current, [field.name]: value }))
            }
            placeholder={
              connected
                ? "Enter a new API key to update"
                : "Enter provider API key"
            }
            label="API Key"
          />
        ) : (
          <div key={field.name}>
            <Label htmlFor={`llm-connection-${field.name}-${connection.presetId}`} className="text-white">
              {field.label}
            </Label>
            <div className="relative mt-2">
              <Input
                id={`llm-connection-${field.name}-${connection.presetId}`}
                type={
                  field.fieldType === "password"
                    ? "password"
                    : field.fieldType === "url"
                      ? "url"
                      : "text"
                }
                value={fieldValues[field.name] ?? ""}
                onChange={(event) =>
                  setFieldValues((current) => ({
                    ...current,
                    [field.name]: event.target.value,
                  }))
                }
                autoComplete="off"
                className="border-slate-600 bg-slate-800 pr-10 text-white"
              />
              {field.secret ? (
                <Key className="absolute right-3 top-2.5 h-4 w-4 text-slate-500" />
              ) : null}
            </div>
          </div>
        )
      ))}

      <div className="flex flex-wrap gap-3">
        <Button
          type="button"
          aria-label={`${connected ? "Update" : "Connect"} ${providerLabel}`}
          onClick={connect}
          disabled={isPending || !requiredFieldsComplete}
          className="bg-blue-600 hover:bg-blue-700"
        >
          {isPending ? (
            <Loader2 className="h-4 w-4 animate-spin" />
          ) : (
            <Key className="h-4 w-4" />
          )}
          {connected ? "Update" : "Connect"}
        </Button>
        {connected ? (
          <Button
            type="button"
            aria-label={`Disconnect ${providerLabel}`}
            onClick={disconnect}
            disabled={isPending}
            variant="outline"
            className="border-slate-600 text-gray-300 hover:text-white"
          >
            {isPending ? (
              <Loader2 className="h-4 w-4 animate-spin" />
            ) : (
              <Trash2 className="h-4 w-4" />
            )}
            Disconnect
          </Button>
        ) : null}
      </div>
    </ProviderSettingsCard>
  );
}

function fallbackFieldLabel(name: string): string {
  if (name === "api_key") {
    return "API key";
  }
  if (name === "display_label") {
    return "Display name";
  }
  return name;
}

export default ConnectionSettingsPanel;
