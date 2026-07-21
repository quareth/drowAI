/**
 * Shared visual structure and API-key control for LLM provider settings.
 *
 * Keeps status placement, credential visibility, and card spacing consistent
 * while provider-specific components retain ownership of their API behavior.
 */
import { useState, type ReactNode } from "react";
import { AlertCircle, CheckCircle, Eye, EyeOff, Key } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";

interface ProviderSettingsCardProps {
  title: string;
  setupNote?: string | null;
  statusLabel: string;
  statusPositive: boolean;
  headerDetails?: ReactNode;
  children: ReactNode;
}

export function ProviderSettingsCard({
  title,
  setupNote = null,
  statusLabel,
  statusPositive,
  headerDetails = null,
  children,
}: ProviderSettingsCardProps) {
  return (
    <Card
      role="group"
      aria-label={`${title} provider settings`}
      className="border-slate-700 bg-slate-900"
    >
      <CardHeader className="space-y-3">
        <div className="flex items-start justify-between gap-3">
          <CardTitle className="flex min-w-0 items-center text-base text-white">
            <Key className="mr-2 h-4 w-4 shrink-0" />
            {title}
          </CardTitle>
          <div
            aria-label={`${title} status: ${statusLabel}`}
            className="flex shrink-0 items-center gap-2"
          >
            {statusPositive ? (
              <CheckCircle className="h-4 w-4 text-green-500" />
            ) : (
              <AlertCircle className="h-4 w-4 text-yellow-500" />
            )}
            <Badge
              variant={statusPositive ? "default" : "secondary"}
              className={
                statusPositive
                  ? "bg-green-600 text-white"
                  : "bg-slate-700 text-gray-400"
              }
            >
              {statusLabel}
            </Badge>
          </div>
        </div>
        {setupNote ? <p className="text-xs text-slate-400">{setupNote}</p> : null}
        {headerDetails}
      </CardHeader>
      <CardContent className="space-y-4">{children}</CardContent>
    </Card>
  );
}

interface ProviderApiKeyFieldProps {
  id: string;
  value: string;
  onChange: (value: string) => void;
  placeholder: string;
  label?: string;
}

export function ProviderApiKeyField({
  id,
  value,
  onChange,
  placeholder,
  label = "API Key",
}: ProviderApiKeyFieldProps) {
  const [showKey, setShowKey] = useState(false);

  return (
    <div>
      <Label htmlFor={id} className="text-white">
        {label}
      </Label>
      <div className="relative mt-2">
        <Input
          id={id}
          type={showKey ? "text" : "password"}
          value={value}
          onChange={(event) => onChange(event.target.value)}
          placeholder={placeholder}
          autoComplete="off"
          className="border-slate-600 bg-slate-800 pr-12 text-white"
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
  );
}
