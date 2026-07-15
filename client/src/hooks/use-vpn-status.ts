import { useCallback, useEffect, useState } from 'react';
import { useWebSocket } from '@/hooks/use-websocket';
import { apiFetch } from '@/lib/api-config';

export interface VPNStatusMessage {
  type: 'vpn_status_update';
  data: {
    task_id: number;
    status: 'configured' | 'disconnected' | 'connecting' | 'connected' | 'failed' | 'reconnecting';
    ip_address?: string;
    location?: string;
    latency?: number;
    error_message?: string;
    connection_time?: string;
    last_seen?: string;
  };
  timestamp: string;
}

export function useVPNStatus(taskId: number) {
  const [vpnStatus, setVpnStatus] = useState<VPNStatusMessage | null>(null);

  const refreshStatus = useCallback(async () => {
    if (!taskId) return null;
    const response = await apiFetch(`/api/tasks/${taskId}/vpn/status`, { method: 'GET' });
    if (response.status === 404) {
      setVpnStatus(null);
      return null;
    }
    if (!response.ok) {
      throw new Error(`VPN status request failed (${response.status})`);
    }
    const payload = await response.json();
    const message: VPNStatusMessage = {
      type: 'vpn_status_update',
      timestamp: new Date().toISOString(),
      data: {
        task_id: taskId,
        status: payload.connection_status,
        ip_address: payload.ip_address || undefined,
        error_message: payload.error_message || undefined,
        connection_time: payload.connected_at || undefined,
      },
    };
    setVpnStatus(message);
    return message;
  }, [taskId]);

  useEffect(() => {
    void refreshStatus().catch(() => undefined);
  }, [refreshStatus]);

  useEffect(() => {
    const status = vpnStatus?.data.status;
    if (!status || !['configured', 'connecting', 'reconnecting'].includes(status)) return;
    const interval = window.setInterval(() => {
      void refreshStatus().catch(() => undefined);
    }, 3000);
    return () => window.clearInterval(interval);
  }, [refreshStatus, vpnStatus?.data.status]);

  const { isConnected } = useWebSocket({
    url: `/ws?type=vpn_status&taskId=${taskId}`,
    onMessage: (event) => {
      try {
        const message = JSON.parse(event.data);
        if (message.type === 'vpn_status_update') {
          setVpnStatus(message as VPNStatusMessage);
        }
      } catch (e) {
        // ignore non-json
      }
    },
    enabled: !!taskId,
  });

  return { vpnStatus, isConnected, refreshStatus };
}
