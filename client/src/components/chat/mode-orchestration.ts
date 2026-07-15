import type { UseMutationResult } from "@tanstack/react-query";

import type { RuntimeModelSwitchPayload } from "@/features/llm-provider/types";
import type { SendMessageFn } from "./types";

export type SendMessagePayload = string | { message: string; client_message_id?: string };
import type { ModeStrategy } from "./mode-strategies";

export interface SSEConnectionController {
  isConnected: boolean;
  reconnect: () => void | Promise<void>;
  disconnect: () => void | Promise<void>;
}

export interface ModeOrchestrationContract {
  readonly mode: "interactive";
  readonly sendMessageMutation?: UseMutationResult<unknown, Error, SendMessagePayload, unknown>;
  readonly switchTaskModelMutation?: UseMutationResult<
    unknown,
    Error,
    RuntimeModelSwitchPayload,
    unknown
  >;
  readonly sseConnection: SSEConnectionController;

  setStrategy(strategy: ModeStrategy): void;
  orchestrateMessageFlow(
    message: string,
    mode: "interactive",
    options?: { skipOptimistic?: boolean; clientMessageId?: string },
  ): Promise<void>;
  handleSSEReconnect(mode: "interactive"): Promise<void>;
  validateModeTransition(from: "interactive", to: "interactive"): boolean;
}

export interface BaseOrchestrationCallbacks {
  onOptimisticMessage?: (content: string) => string | undefined;
  onMessageSettled?: (optimisticId?: string) => void;
  onMessageError?: (error: Error, optimisticId?: string) => void;
  logger?: (level: "debug" | "info" | "warn" | "error", message: string, meta?: Record<string, unknown>) => void;
}

export interface InteractiveOrchestrationOptions extends BaseOrchestrationCallbacks {
  sendMessageMutation?: UseMutationResult<unknown, Error, SendMessagePayload, unknown>;
  switchTaskModelMutation?: UseMutationResult<
    unknown,
    Error,
    RuntimeModelSwitchPayload,
    unknown
  >;
  sendMessage?: SendMessageFn;
  sseConnection: SSEConnectionController;
}

abstract class BaseModeOrchestration implements ModeOrchestrationContract {
  public abstract readonly mode: "interactive";

  public readonly sendMessageMutation?: UseMutationResult<unknown, Error, SendMessagePayload, unknown>;
  public readonly switchTaskModelMutation?: UseMutationResult<
    unknown,
    Error,
    RuntimeModelSwitchPayload,
    unknown
  >;

  public readonly sseConnection: SSEConnectionController;

  protected strategy: ModeStrategy | null = null;
  protected readonly callbacks: BaseOrchestrationCallbacks;

  protected constructor(
    base: {
      sendMessageMutation?: UseMutationResult<unknown, Error, SendMessagePayload, unknown>;
      switchTaskModelMutation?: UseMutationResult<
        unknown,
        Error,
        RuntimeModelSwitchPayload,
        unknown
      >;
      sseConnection: SSEConnectionController;
      callbacks?: BaseOrchestrationCallbacks;
    },
  ) {
    this.sendMessageMutation = base.sendMessageMutation;
    this.switchTaskModelMutation = base.switchTaskModelMutation;
    this.sseConnection = base.sseConnection;
    this.callbacks = base.callbacks ?? {};
  }

  public setStrategy(strategy: ModeStrategy): void {
    this.strategy = strategy;
  }

  public abstract orchestrateMessageFlow(
    message: string,
    mode: "interactive",
    options?: { skipOptimistic?: boolean; clientMessageId?: string },
  ): Promise<void>;

  public async handleSSEReconnect(mode: "interactive"): Promise<void> {
    if (mode !== this.mode) return;
    try {
      await Promise.resolve(this.sseConnection.reconnect());
    } catch (error) {
      this.callbacks.logger?.("error", "Failed to reconnect SSE stream", {
        error,
        mode,
      });
      throw error instanceof Error ? error : new Error(String(error));
    }
  }

  public validateModeTransition(from: "interactive", to: "interactive"): boolean {
    return from === to;
  }

  protected handleOptimisticSuccess(optimisticId?: string): void {
    try {
      this.callbacks.onMessageSettled?.(optimisticId);
    } catch (error) {
      this.callbacks.logger?.("warn", "onMessageSettled callback threw", { error });
    }
  }

  protected handleOptimisticError(error: Error, optimisticId?: string): void {
    try {
      this.callbacks.onMessageError?.(error, optimisticId);
    } catch (callbackError) {
      this.callbacks.logger?.("warn", "onMessageError callback threw", {
        error: callbackError,
      });
    }
  }

  protected emitOptimisticMessage(content: string): string | undefined {
    try {
      return this.callbacks.onOptimisticMessage?.(content);
    } catch (error) {
      this.callbacks.logger?.("warn", "onOptimisticMessage callback threw", { error });
      return undefined;
    }
  }
}

export class InteractiveModeOrchestration extends BaseModeOrchestration {
  public readonly mode = "interactive" as const;

  private readonly sendMessage: SendMessageFn | null;

  constructor(options: InteractiveOrchestrationOptions) {
    super({
      sendMessageMutation: options.sendMessageMutation,
      switchTaskModelMutation: options.switchTaskModelMutation,
      sseConnection: options.sseConnection,
      callbacks: options,
    });
    this.sendMessage = options.sendMessage ?? null;
  }

  public override setStrategy(strategy: ModeStrategy): void {
    if (strategy.type !== "interactive") {
      throw new Error("InteractiveModeOrchestration requires an interactive strategy");
    }
    super.setStrategy(strategy);
  }

  public async orchestrateMessageFlow(
    message: string,
    mode: "interactive",
    options?: { skipOptimistic?: boolean; clientMessageId?: string },
  ): Promise<void> {
    if (mode !== this.mode) {
      this.callbacks.logger?.("warn", "Ignoring message for mismatched mode", {
        expectedMode: this.mode,
        receivedMode: mode,
      });
      return;
    }

    if (!this.strategy) {
      throw new Error("InteractiveModeOrchestration requires a strategy before use");
    }

    const validationError = this.strategy.validateInput(message);
    if (validationError) {
      throw new Error(validationError);
    }

    const trimmed = message.trim();
    const optimisticId = options?.skipOptimistic ? undefined : this.emitOptimisticMessage(trimmed);

    try {
      if (this.sendMessageMutation) {
        if (options?.clientMessageId) {
          await this.sendMessageMutation.mutateAsync({
            message: trimmed,
            client_message_id: options.clientMessageId,
          } as any);
        } else {
          await this.sendMessageMutation.mutateAsync(trimmed);
        }
      } else if (this.sendMessage) {
        await this.sendMessage(trimmed);
      } else {
        await this.strategy.handleMessageSend(trimmed);
      }
      this.handleOptimisticSuccess(optimisticId);
    } catch (error) {
      const normalizedError = error instanceof Error ? error : new Error(String(error));
      if (optimisticId) {
        this.handleOptimisticError(normalizedError, optimisticId);
      }
      throw normalizedError;
    }
  }

  public override async handleSSEReconnect(mode: "interactive"): Promise<void> {
    if (mode !== this.mode) return;

    if (this.switchTaskModelMutation?.isPending) {
      this.callbacks.logger?.("info", "Delaying SSE reconnect until model switch completes");
      return;
    }

    await super.handleSSEReconnect(mode);
  }

  public override validateModeTransition(from: "interactive", to: "interactive"): boolean {
    return from === this.mode && to === this.mode;
  }
}
