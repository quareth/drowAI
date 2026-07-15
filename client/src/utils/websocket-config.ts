/**
 * Environment-agnostic WebSocket URL configuration
 * Automatically detects the correct backend URL for WebSocket connections
 */

interface WebSocketConfig {
  getWebSocketUrl: (path: string, params?: Record<string, string>) => string;
}

class EnvironmentWebSocketConfig implements WebSocketConfig {
  private backendUrl: string | null = null;

  private detectBackendUrl(): string {
    if (this.backendUrl) return this.backendUrl;

    const isDev = import.meta.env.DEV;
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const host = window.location.host; // hostname:port

    // In dev, use same-origin so Vite proxy handles routing to backend:8000
    if (isDev) {
      this.backendUrl = `${protocol}//${host}`;
      return this.backendUrl;
    }

    // In prod, default to same-origin; allow override via VITE_WS_BASE if needed
    const envBase = (import.meta as any).env?.VITE_WS_BASE as string | undefined;
    this.backendUrl = envBase ? envBase : `${protocol}//${host}`;
    return this.backendUrl;
  }

  getWebSocketUrl(path: string, params: Record<string, string> = {}): string {
    const baseUrl = this.detectBackendUrl();
    
    // Ensure path starts with /
    const normalizedPath = path.startsWith('/') ? path : `/${path}`;
    
    // Build query string
    const queryParams = new URLSearchParams(params);
    const queryString = queryParams.toString();
    
    // Combine URL parts
    const separator = normalizedPath.includes('?') ? '&' : '?';
    const finalUrl = queryString 
      ? `${baseUrl}${normalizedPath}${separator}${queryString}`
      : `${baseUrl}${normalizedPath}`;
    
    return finalUrl;
  }
}

// Export singleton instance
export const wsConfig = new EnvironmentWebSocketConfig();

// Convenience function for common WebSocket endpoint
export function getWebSocketUrl(type: string, taskId: number): string {
  return wsConfig.getWebSocketUrl('/ws', {
    type,
    taskId: taskId.toString()
  });
}