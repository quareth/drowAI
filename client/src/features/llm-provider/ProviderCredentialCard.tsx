/**
 * Reusable credential editor for one LLM provider.
 *
 * Handles non-secret credential status and one-action connect/disconnect
 * operations through provider-neutral LLM APIs without echoing stored keys.
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
  setupNote?: string | null;
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
  setupNote = null,
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

  const connectMutation = useMutation({
    mutationFn: async () => {
      const trimmedKey = apiKey.trim();
      if (!trimmedKey) {
        throw new Error("Enter an API key before connecting.");
      }
      const status = await saveLLMProviderCredential(provider.id, {
        api_key: trimmedKey,
        enabled: true,
      });
      await testLLMProviderCredential(provider.id, { api_key: trimmedKey });
      return status;
    },
    onSuccess: async () => {
      setApiKey("");
      await invalidateCredentialState();
      onSuccess(
        `${provider.label} connected`,
        "The provider credential is ready for runtime use.",
      );
    },
    onError: (error) => onError(
      `${provider.label} connection failed`,
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
  const isBusy = connectMutation.isPending || deleteMutation.isPending;

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
                <Badge className="bg-green-600 text-white">Connected</Badge>
              </>
            ) : (
              <>
                <AlertCircle className="h-4 w-4 text-yellow-500" />
                <Badge variant="secondary" className="bg-slate-700 text-gray-400">Not connected</Badge>
              </>
            )}
          </div>
        </div>
        {setupNote ? (
          <p className="text-xs text-slate-400">{setupNote}</p>
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
            aria-label={`${hasStoredCredential ? "Update" : "Connect"} ${provider.label}`}
            onClick={() => { connectMutation.mutate(); }}
            disabled={isBusy || !apiKey.trim()}
            className="bg-blue-600 hover:bg-blue-700"
          >
            {connectMutation.isPending ? (
              <Loader2 className="h-4 w-4 animate-spin" />
            ) : (
              <Key className="h-4 w-4" />
            )}
            {hasStoredCredential ? "Update" : "Connect"}
          </Button>
          {hasStoredCredential ? (
            <Button
              aria-label={`Disconnect ${provider.label}`}
              onClick={() => { deleteMutation.mutate(); }}
              disabled={isBusy}
              variant="outline"
              className="border-slate-600 text-gray-300 hover:text-white"
            >
              {deleteMutation.isPending ? (
                <Loader2 className="h-4 w-4 animate-spin" />
              ) : (
                <Trash2 className="h-4 w-4" />
              )}
              Disconnect
            </Button>
          ) : null}
        </div>
      </CardContent>
    </Card>
  );
}

export default ProviderCredentialCard;
