/**
 * Display preference step for the standalone setup wizard.
 */
import { Globe } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { TIMEZONE_OPTIONS } from "@/components/settings/lib/timezone-options";
import { DEFAULT_TIMEZONE } from "@/hooks/use-user-timezone";
import { SetupActions, SetupStepHeader } from "@/components/setup/setup-ui";
import type { SetupDisplayConfig } from "@/components/setup/setup-types";

interface DisplayStepProps {
  config: SetupDisplayConfig;
  onUpdate: (data: Partial<SetupDisplayConfig>) => void;
  onNext: () => void;
  onPrevious: () => void;
}

export function DisplayStep({ config, onUpdate, onNext, onPrevious }: DisplayStepProps) {
  const timezone = config.timezone || DEFAULT_TIMEZONE;

  return (
    <div className="space-y-6">
      <SetupStepHeader
        icon={Globe}
        title="Display"
        description="Set the timezone applied to the first admin account and date displays."
      />

      <div className="space-y-2">
        <Label htmlFor="timezone">Timezone</Label>
        <Select value={timezone} onValueChange={(value) => onUpdate({ timezone: value })}>
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
        <p className="text-xs text-slate-500 dark:text-slate-400">
          All dates and times across the application will be shown in this timezone.
        </p>
      </div>

      <SetupActions>
        <Button variant="outline" onClick={onPrevious} className="border-slate-700 bg-slate-950 text-slate-200 hover:bg-slate-800 hover:text-white">
          Previous
        </Button>
        <Button onClick={onNext}>Next</Button>
      </SetupActions>
    </div>
  );
}
