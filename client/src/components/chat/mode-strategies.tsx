import type { ReactNode } from "react";

import type { ChatMode, SendMessageFn } from "./types";

export interface ModeStrategy {
  readonly type: "interactive";
  readonly canSendMessages: boolean;

  /**
   * Derived UI metadata for the chat input and auxiliary indicators.
   */
  getModeConfig(): ChatMode;

  /**
   * Return a React node to render next to the chat input for quick mode context.
   */
  getModeIndicator(): ReactNode;

  /**
   * Validate outgoing user content. Return an error string or null when valid.
   */
  validateInput(content: string): string | null;

  /**
   * Dispatch a message through the appropriate pipeline for the strategy.
   */
  handleMessageSend(content: string): Promise<void>;
}

export interface InteractiveModeOptions {
  sendMessage: SendMessageFn;
  inputPlaceholder?: string;
}

class InteractiveModeStrategy implements ModeStrategy {
  public readonly type = "interactive" as const;
  public readonly canSendMessages = true;

  private readonly sendMessage: SendMessageFn;
  private readonly placeholder: string;

  constructor(options: InteractiveModeOptions) {
    if (!options.sendMessage) {
      throw new Error("InteractiveModeStrategy requires a sendMessage function");
    }
    this.sendMessage = options.sendMessage;
    this.placeholder = options.inputPlaceholder ?? "Type a message (Enter to send)…";
  }

  getModeConfig(): ChatMode {
    return {
      canSendMessages: true,
      inputPlaceholder: this.placeholder,
      inputDisabled: false,
    };
  }

  getModeIndicator(): ReactNode {
    return "Interactive mode";
  }

  validateInput(content: string): string | null {
    if (!content.trim()) {
      return "Message cannot be empty";
    }
    return null;
  }

  async handleMessageSend(content: string): Promise<void> {
    const error = this.validateInput(content);
    if (error) {
      return Promise.reject(new Error(error));
    }
    await this.sendMessage(content.trim());
  }
}

export type ModeStrategyOptions = { type: "interactive" } & InteractiveModeOptions;

export function createModeStrategy(options: ModeStrategyOptions): ModeStrategy {
  return new InteractiveModeStrategy(options);
}

export { InteractiveModeStrategy };

// ---------------------------
// Basic Chat Strategy helper
// ---------------------------

export interface BasicChatOptions extends InteractiveModeOptions {
  indicatorText?: string;
  inputPlaceholder?: string;
}

class BasicChatStrategy implements ModeStrategy {
  public readonly type = "interactive" as const;
  public readonly canSendMessages = true;

  private readonly sendMessage: SendMessageFn;
  private readonly placeholder: string;
  private readonly indicator: ReactNode;

  constructor(options: BasicChatOptions) {
    if (!options.sendMessage) {
      throw new Error("BasicChatStrategy requires a sendMessage function");
    }
    this.sendMessage = options.sendMessage;
    this.placeholder = options.inputPlaceholder ?? "Chat with AI (Enter to send)…";
    this.indicator = options.indicatorText ?? "Basic Chat";
  }

  getModeConfig(): ChatMode {
    return {
      canSendMessages: true,
      inputPlaceholder: this.placeholder,
      inputDisabled: false,
    };
  }

  getModeIndicator(): ReactNode {
    return this.indicator;
  }

  validateInput(content: string): string | null {
    if (!content.trim()) return "Message cannot be empty";
    return null;
  }

  async handleMessageSend(content: string): Promise<void> {
    const error = this.validateInput(content);
    if (error) {
      return Promise.reject(new Error(error));
    }
    await this.sendMessage(content.trim());
  }
}

export function createBasicChatStrategy(options: BasicChatOptions): ModeStrategy {
  return new BasicChatStrategy(options);
}
