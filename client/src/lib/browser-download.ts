/**
 * Browser download helpers for client-generated files.
 *
 * Responsibility: centralize Blob URL creation and link-click cleanup so
 * feature components can trigger local downloads without duplicating DOM code.
 */

export function triggerBrowserDownload(blob: Blob, filename: string): void {
  const url = window.URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  link.style.display = "none";
  document.body.appendChild(link);
  link.click();
  document.body.removeChild(link);
  window.URL.revokeObjectURL(url);
}

export function downloadTextFile(
  content: string,
  filename: string,
  mimeType = "text/plain;charset=utf-8",
): void {
  triggerBrowserDownload(new Blob([content], { type: mimeType }), filename);
}
