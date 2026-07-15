export function formatToolName(raw: string | undefined | null): string {
  if (!raw) return "Unknown tool";

  // If the ID is namespaced (a.b.c), use the last segment
  const segments = raw.split(".");
  const last = segments[segments.length - 1] || raw;

  // Replace underscores with spaces and capitalize each word
  return last
    .split(/[_-]+/)
    .filter(Boolean)
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(" ");
}


