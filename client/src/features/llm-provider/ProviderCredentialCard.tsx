/**
 * Reusable credential editor for one LLM provider.
 *
 * Handles non-secret credential status, save, test, and delete actions through
 * provider-neutral LLM APIs without echoing stored keys back to the browser.
 */
import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { AlertCircle, CheckCircle, Eye, EyeOff, Key, Loader2, Trash2 } from "lucide-react";

import {
  deleteLLMProviderCredential,
  saveLLMProviderCredential,
  testLLMProviderCredential,
} from "@/features/llm-provider/api";
import type { LLMCatalogProvider } from "@/features/llm-provider/types";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";

export interface ProviderCredentialCardProps {
  provider: LLMCatalogProvider;
  onSuccess: (title: string, description: string) => void;
  onError: (title: string, error: Error) => void;
}

const catalogQueryKey = ["/api/llm/models"] as const;

function getCredentialPlaceholder(provider: LLMCatalogProvider): string {
  if (provider.credential.has_api_key) {
    return "Enter a new API key to update";
  }
  return provider.id === "openai" ? "sk-..." : "Enter provider API key";
}

export function ProviderCredentialCard({
  provider,
  onSuccess,
  onError,
}: ProviderCredentialCardProps) {
  const queryClient = useQueryClient();
  const [apiKey, setApiKey] = useState("");
  const [showKey, setShowKey] = useState(false);

  const invalidateCredentialState = async () => {
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: catalogQueryKey }),
      queryClient.invalidateQueries({
        queryKey: ["/api/llm/providers", provider.id, "credential"],
      }),
    ]);
  };

  const saveMutation = useMutation({
    mutationFn: async () => {
      const trimmedKey = apiKey.trim();
      if (!trimmedKey) {
        throw new Error("Enter an API key before saving.");
      }
      return saveLLMProviderCredential(provider.id, {
        api_key: trimmedKey,
        enabled: true,
      });
    },
    onSuccess: async () => {
      setApiKey("");
      await invalidateCredentialState();
      onSuccess(
        `${provider.label} credential saved`,
        "The stored credential status has been updated.",
      );
    },
    onError: (error) => onError(
      `${provider.label} credential save failed`,
      error instanceof Error ? error : new Error(String(error)),
    ),
  });

  const testMutation = useMutation({
    mutationFn: async () => {
      const trimmedKey = apiKey.trim();
      return testLLMProviderCredential(
        provider.id,
        trimmedKey ? { api_key: trimmedKey } : {},
      );
    },
    onSuccess: (result) => {
      onSuccess(
        `${provider.label} connection verified`,
        result.model_count != null
          ? `Connection verified. Found ${result.model_count} available models.`
          : result.message,
      );
    },
    onError: (error) => onError(
      `${provider.label} connection test failed`,
      error instanceof Error ? error : new Error(String(error)),
    ),
  });

  const deleteMutation = useMutation({
    mutationFn: () => deleteLLMProviderCredential(provider.id),
    onSuccess: async () => {
      setApiKey("");
      await invalidateCredentialState();
      onSuccess(
        `${provider.label} credential removed`,
        "The provider credential has been disabled for runtime use.",
      );
    },
    onError: (error) => onError(
      `${provider.label} credential delete failed`,
      error instanceof Error ? error : new Error(String(error)),
    ),
  });

  const hasStoredCredential = provider.credential.enabled && provider.credential.has_api_key;
  const canTest = Boolean(apiKey.trim()) || hasStoredCredential;
  const isBusy = saveMutation.isPending || testMutation.isPending || deleteMutation.isPending;

  return (
    <Card className="bg-slate-900 border-slate-700">
      <CardHeader className="space-y-3">
        <div className="flex items-center justify-between gap-3">
          <CardTitle className="flex items-center text-base text-white">
            <Key className="mr-2 h-4 w-4" />
            {provider.label}
          </CardTitle>
          <div className="flex items-center gap-2">
            {hasStoredCredential ? (
              <>
                <CheckCircle className="h-4 w-4 text-green-500" />
                <Badge className="bg-green-600 text-white">Configured</Badge>
              </>
            ) : (
              <>
                <AlertCircle className="h-4 w-4 text-yellow-500" />
                <Badge variant="secondary" className="bg-slate-700 text-gray-400">Not Set</Badge>
              </>
            )}
          </div>
        </div>
        {provider.credential.masked_api_key ? (
          <p className="text-xs text-slate-400">
            Stored key: <span className="font-mono">{provider.credential.masked_api_key}</span>
          </p>
        ) : null}
      </CardHeader>
      <CardContent className="space-y-4">
        <div>
          <Label htmlFor={`llm-provider-key-${provider.id}`} className="text-white">
            API Key
          </Label>
          <div className="relative mt-2">
            <Input
              id={`llm-provider-key-${provider.id}`}
              type={showKey ? "text" : "password"}
              value={apiKey}
              onChange={(event) => setApiKey(event.target.value)}
              placeholder={getCredentialPlaceholder(provider)}
              autoComplete="off"
              className="bg-slate-800 border-slate-600 pr-12 text-white"
            />
            <Button
              type="button"
              variant="ghost"
              size="sm"
              onClick={() => setShowKey((current) => !current)}
              className="absolute inset-y-0 right-0 h-full px-3 text-gray-400 hover:text-white"
              aria-label={showKey ? "Hide API key" : "Show API key"}
            >
              {showKey ? <EyeOff className="h-4 w-4" /> : <Eye className="h-4 w-4" />}
            </Button>
          </div>
        </div>

        <div className="flex flex-wrap gap-3">
          <Button
            onClick={() => { saveMutation.mutate(); }}
            disabled={isBusy || !apiKey.trim()}
            className="bg-blue-600 hover:bg-blue-700"
          >
            {saveMutation.isPending ? (
              <Loader2 className="h-4 w-4 animate-spin" />
            ) : (
              <Key className="h-4 w-4" />
            )}
            Save
          </Button>
          <Button
            onClick={() => { testMutation.mutate(); }}
            disabled={isBusy || !canTest}
            variant="outline"
            className="border-slate-600 text-gray-300 hover:text-white"
          >
            {testMutation.isPending ? (
              <Loader2 className="h-4 w-4 animate-spin" />
            ) : (
              <CheckCircle className="h-4 w-4" />
            )}
            Test
          </Button>
          <Button
            onClick={() => { deleteMutation.mutate(); }}
            disabled={isBusy || !hasStoredCredential}
            variant="outline"
            className="border-slate-600 text-gray-300 hover:text-white"
          >
            {deleteMutation.isPending ? (
              <Loader2 className="h-4 w-4 animate-spin" />
            ) : (
              <Trash2 className="h-4 w-4" />
            )}
            Delete
          </Button>
        </div>
      </CardContent>
    </Card>
  );
}

export default ProviderCredentialCard;
