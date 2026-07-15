/**
 * Display settings panel for timezone preferences.
 *
 * Scope:
 * - Loads current timezone from API settings query data.
 * - Persists timezone changes via updateSettings mutation.
 *
 * Boundary:
 * - UI-only settings panel; no direct API calls.
 * - Delegates data access/mutation to useApiSettings.
 */
import { useEffect, useState } from "react";
import { Globe, Loader2 } from "lucide-react";

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { TIMEZONE_OPTIONS } from "@/components/settings/lib/timezone-options";
import { useApiSettings } from "@/hooks/use-api-settings";
import { DEFAULT_TIMEZONE } from "@/hooks/use-user-timezone";
import { useToast } from "@/hooks/use-toast";

type DisplaySettingsPanelProps = {
  queryEnabled: boolean;
};

export function DisplaySettingsPanel({ queryEnabled }: DisplaySettingsPanelProps) {
  const { toast } = useToast();
  const [timezone, setTimezone] = useState(DEFAULT_TIMEZONE);
  const { settingsQuery, updateSettings, updateSettingsMutation } = useApiSettings({ enabled: queryEnabled });

  useEffect(() => {
    setTimezone(settingsQuery.data?.timezone ?? DEFAULT_TIMEZONE);
  }, [settingsQuery.data?.timezone]);

  const handleTimezoneChange = async (selectedTimezone: string) => {
    const previousTimezone = timezone;
    setTimezone(selectedTimezone);

    try {
      await updateSettings({ timezone: selectedTimezone });
      toast({
        title: "Display settings updated",
        description: "Timezone preference saved successfully.",
      });
    } catch (error) {
      setTimezone(previousTimezone);
      toast({
        title: "Failed to update timezone",
        description: error instanceof Error ? error.message : "Could not save timezone preference.",
        variant: "destructive",
      });
    }
  };

  return (
    <Card className="bg-slate-900 border-slate-700">
      <CardHeader>
        <CardTitle className="text-white flex items-center">
          <Globe className="w-5 h-5 mr-2" />
          Display Preferences
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-4">
        {settingsQuery.isLoading ? (
          <div className="flex items-center justify-center py-8">
            <Loader2 className="w-6 h-6 animate-spin text-gray-400" />
          </div>
        ) : (
          <div className="space-y-2">
            <Label htmlFor="timezone" className="text-white">
              Timezone
            </Label>
            <Select
              value={timezone}
              onValueChange={(value) => {
                void handleTimezoneChange(value);
              }}
              disabled={updateSettingsMutation.isPending}
            >
              <SelectTrigger id="timezone" className="w-full">
                <SelectValue placeholder="Select timezone" />
              </SelectTrigger>
              <SelectContent>
                {TIMEZONE_OPTIONS.map((option) => (
                  <SelectItem key={option.value} value={option.value}>
                    {option.label}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            <p className="text-xs text-gray-400">
              All dates and times across the application will be shown in this timezone.
            </p>
          </div>
        )}
      </CardContent>
    </Card>
  );
}
