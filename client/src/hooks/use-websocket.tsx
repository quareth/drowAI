// @ts-nocheck
/**
 * Generic WebSocket hook for lightweight channel subscriptions.
 *
 * Responsibilities:
 * - establish one authenticated websocket transport for the requested URL
 * - surface socket lifecycle callbacks to callers
 * - recreate the transport when tenant context changes
 */
import { useEffect, useRef, useState } from "react";
import { wsConfig } from "../utils/websocket-config";
import {
  CHANNEL_TRANSPORT_DEFAULTS,
  ChannelWebSocketTransport,
  createChannelWebSocketTransportConfig,
} from "@/services/runtime_stream/ChannelWebSocketTransport";
import { onActiveTenantChanged } from "@/lib/tenant-context";

interface UseWebSocketOptions {
  url: string;
  enabled?: boolean;
  onMessage?: (event: MessageEvent) => void;
  onOpen?: () => void;
  onClose?: () => void;
  onError?: (error: Event) => void;
}

export function useWebSocket({
  url,
  enabled = true,
  onMessage,
  onOpen,
  onClose,
  onError,
}: UseWebSocketOptions) {
  const [socket, setSocket] = useState<WebSocket | null>(null);
  const [isConnected, setIsConnected] = useState(false);
  const [tenantSwitchEpoch, setTenantSwitchEpoch] = useState(0);
  const transportRef = useRef<ChannelWebSocketTransport | null>(null);
  const callbacksRef = useRef({
    onMessage,
    onOpen,
    onClose,
    onError,
  });

  useEffect(() => {
    callbacksRef.current = {
      onMessage,
      onOpen,
      onClose,
      onError,
    };
  }, [onMessage, onOpen, onClose, onError]);

  useEffect(() => {
    return onActiveTenantChanged(() => {
      setTenantSwitchEpoch((current) => current + 1);
    });
  }, []);

  useEffect(() => {
    if (!enabled || !url) return;

    const isAbsolute = /^wss?:\/\//i.test(url);
    const wsUrl = isAbsolute ? url : wsConfig.getWebSocketUrl(url);
    const transport = new ChannelWebSocketTransport(createChannelWebSocketTransportConfig({
      url: wsUrl,
      runtimeDefaults: CHANNEL_TRANSPORT_DEFAULTS,
      enableReconnect: false,
      onMissingToken: () => {
        if (transportRef.current !== transport) return;
        setIsConnected(false);
        setSocket(null);
      },
      onOpen: (openedSocket) => {
        if (transportRef.current !== transport) return;
        openedSocket.binaryType = "arraybuffer";
        setSocket(openedSocket);
        setIsConnected(true);
        callbacksRef.current.onOpen?.();
      },
      onMessage: (event) => {
        if (transportRef.current !== transport) return;
        callbacksRef.current.onMessage?.(event);
      },
      onClose: () => {
        if (transportRef.current !== transport) return;
        setIsConnected(false);
        setSocket(null);
        callbacksRef.current.onClose?.();
      },
      onError: (event) => {
        if (transportRef.current !== transport) return;
        callbacksRef.current.onError?.(event);
      },
    }));
    transportRef.current = transport;
    transport.connect();

    return () => {
      setIsConnected(false);
      setSocket(null);
      transport.disconnect(1000, "hook cleanup");
      if (transportRef.current === transport) {
        transportRef.current = null;
      }
    };
  }, [url, enabled, tenantSwitchEpoch]);

  const sendMessage = (data: any) => {
    transportRef.current?.sendJson(data);
  };

  return { socket, isConnected, sendMessage };
}
