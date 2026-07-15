/**
 * Data management settings panel for tenant lifecycle policies.
 */
import { useEffect, useState } from "react";
import { AlertCircle, Database, Save } from "lucide-react";

import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  useDataManagementSettings,
  type DataManagementSettingsUpdateRequest,
} from "@/hooks/use-data-management-settings";
import { useToast } from "@/hooks/use-toast";

type DataManagementSettingsPanelProps = {
  queryEnabled: boolean;
};

function normalizeRetentionDays(value: string): number {
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) {
    return 180;
  }
  return Math.max(1, Math.min(3650, Math.floor(parsed)));
}

export function DataManagementSettingsPanel({
  queryEnabled,
}: DataManagementSettingsPanelProps) {
  const { toast } = useToast();
  const { settingsQuery, updateSettings, updateSettingsMutation } =
    useDataManagementSettings({ enabled: queryEnabled });
  const settings = settingsQuery.data;
  const [retentionDays, setRetentionDays] = useState("180");

  useEffect(() => {
    if (!settings) {
      return;
    }
    setRetentionDays(String(settings.report_history_retention_days));
  }, [settings]);

  const handleSave = async () => {
    const payload: DataManagementSettingsUpdateRequest = {
      report_history_retention_days: normalizeRetentionDays(retentionDays),
    };
    try {
      const updated = await updateSettings(payload);
      setRetentionDays(String(updated.report_history_retention_days));
      toast({
        title: "Data management settings updated",
        description: "Report retention policy was saved for this tenant.",
      });
    } catch (error) {
      toast({
        title: "Update failed",
        description:
          error instanceof Error
            ? error.message
            : "Failed to update data management settings.",
        variant: "destructive",
      });
    }
  };

  return (
    <Card className="bg-slate-900 border-slate-700">
      <CardHeader className="space-y-2">
        <CardTitle className="text-white flex items-center justify-between">
          <span className="flex items-center gap-2">
            <Database className="h-5 w-5" />
            Data Management
          </span>
        </CardTitle>
        <p className="text-sm text-gray-400">
          Configure tenant-level lifecycle policies for generated data.
        </p>
      </CardHeader>
      <CardContent className="space-y-6">
        {settingsQuery.isError ? (
          <Alert variant="destructive">
            <AlertCircle className="h-4 w-4" />
            <AlertTitle>Unable to load data management settings</AlertTitle>
            <AlertDescription>
              {settingsQuery.error?.message || "Try again in a moment."}
            </AlertDescription>
          </Alert>
        ) : null}

        <div className="grid gap-2">
          <Label htmlFor="report-retention-days" className="text-white">
            Historical report retention days
          </Label>
          <Input
            id="report-retention-days"
            type="text"
            inputMode="numeric"
            pattern="[0-9]*"
            value={retentionDays}
            onChange={(event) =>
              setRetentionDays(event.target.value.replace(/\D/g, ""))
            }
            disabled={settingsQuery.isLoading || updateSettingsMutation.isPending}
            className="max-w-xs border-slate-700 bg-slate-950 text-white"
          />
          <p className="text-xs text-gray-400">
            Historical generated reports are automatically erased after this window.
            Current reports are preserved by automatic retention. Manual report
            deletion is always available to users with report delete permission.
          </p>
        </div>

        <Button
          type="button"
          onClick={handleSave}
          disabled={!settings || updateSettingsMutation.isPending}
          className="bg-blue-600 hover:bg-blue-700"
        >
          <Save className="mr-2 h-4 w-4" />
          {updateSettingsMutation.isPending ? "Saving..." : "Save policy"}
        </Button>
      </CardContent>
    </Card>
  );
}
