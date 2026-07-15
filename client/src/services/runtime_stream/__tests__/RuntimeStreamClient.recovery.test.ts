import { describe, expect, it } from "vitest";

import { RuntimeStreamClient } from "../RuntimeStreamClient";

class FakeWebSocket {
  public static CONNECTING = 0;
  public static OPEN = 1;
  public static CLOSING = 2;
  public static CLOSED = 3;

  public readyState = FakeWebSocket.CONNECTING;
  public readonly sentMessages: string[] = [];
  public onopen: (() => void) | null = null;
  public onmessage: ((event: MessageEvent<string>) => void) | null = null;
  public onclose: (() => void) | null = null;
  public onerror: (() => void) | null = null;

  public send(data: string): void {
    this.sentMessages.push(data);
  }

  public close(): void {
    this.readyState = FakeWebSocket.CLOSED;
    this.onclose?.();
  }

  public open(): void {
    this.readyState = FakeWebSocket.OPEN;
    this.onopen?.();
  }

  public emitMessage(payload: unknown): void {
    this.onmessage?.({ data: JSON.stringify(payload) } as MessageEvent<string>);
  }
}

describe("RuntimeStreamClient recovery", () => {
  it("resubscribes from last seen cursor when a sequence gap is detected", () => {
    const sockets: FakeWebSocket[] = [];
    const client = new RuntimeStreamClient({
      url: "ws://example/ws?type=agent-multi",
      tokenProvider: () => "token",
      websocketFactory: () => {
        const socket = new FakeWebSocket();
        sockets.push(socket);
        return socket as unknown as WebSocket;
      },
    });

    client.setDesiredTaskIds([101]);
    client.connect();
    sockets[0].open();

    sockets[0].emitMessage({
      type: "agent_reasoning",
      taskId: 101,
      sequence: 1,
      packet: { type: "status" },
    });
    sockets[0].emitMessage({
      type: "agent_reasoning",
      taskId: 101,
      sequence: 4,
      packet: { type: "status" },
    });

    expect(sockets[0].sentMessages).toContain(
      JSON.stringify({ action: "unsubscribe", channel: "agent", taskId: 101 }),
    );
    expect(sockets[0].sentMessages).toContain(
      JSON.stringify({
        action: "subscribe",
        channel: "agent",
        taskId: 101,
        last_seen_sequence: 1,
      }),
    );
  });

  it("does not trigger recovery on first packet jump without cursor history", () => {
    const sockets: FakeWebSocket[] = [];
    const client = new RuntimeStreamClient({
      url: "ws://example/ws?type=agent-multi",
      tokenProvider: () => "token",
      websocketFactory: () => {
        const socket = new FakeWebSocket();
        sockets.push(socket);
        return socket as unknown as WebSocket;
      },
    });

    client.setDesiredTaskIds([202]);
    client.connect();
    sockets[0].open();

    sockets[0].emitMessage({
      type: "agent_reasoning",
      taskId: 202,
      sequence: 50,
      packet: { type: "status" },
    });

    expect(sockets[0].sentMessages).not.toContain(
      JSON.stringify({ action: "unsubscribe", channel: "agent", taskId: 202 }),
    );
  });
});

