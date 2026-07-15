/**
 * Shared timezone options for display settings and setup wizard.
 */
import { DEFAULT_TIMEZONE } from "@/hooks/use-user-timezone";

export const TIMEZONE_OPTIONS = [
  { value: DEFAULT_TIMEZONE, label: DEFAULT_TIMEZONE },
  { value: "America/New_York", label: "Eastern Time (US)" },
  { value: "America/Chicago", label: "Central Time (US)" },
  { value: "America/Denver", label: "Mountain Time (US)" },
  { value: "America/Los_Angeles", label: "Pacific Time (US)" },
  { value: "America/Anchorage", label: "Alaska" },
  { value: "Pacific/Honolulu", label: "Hawaii" },
  { value: "America/Sao_Paulo", label: "Sao Paulo" },
  { value: "America/Argentina/Buenos_Aires", label: "Buenos Aires" },
  { value: "America/Mexico_City", label: "Mexico City" },
  { value: "America/Toronto", label: "Eastern Time (Canada)" },
  { value: "America/Vancouver", label: "Pacific Time (Canada)" },
  { value: "Europe/London", label: "London" },
  { value: "Europe/Berlin", label: "Berlin / Paris" },
  { value: "Europe/Helsinki", label: "Helsinki / Athens" },
  { value: "Europe/Moscow", label: "Moscow" },
  { value: "Europe/Istanbul", label: "Istanbul" },
  { value: "Asia/Dubai", label: "Dubai" },
  { value: "Asia/Kolkata", label: "India (Kolkata)" },
  { value: "Asia/Bangkok", label: "Bangkok" },
  { value: "Asia/Shanghai", label: "China (Shanghai)" },
  { value: "Asia/Tokyo", label: "Tokyo" },
  { value: "Asia/Seoul", label: "Seoul" },
  { value: "Asia/Singapore", label: "Singapore" },
  { value: "Australia/Sydney", label: "Sydney" },
  { value: "Pacific/Auckland", label: "Auckland" },
] as const;
