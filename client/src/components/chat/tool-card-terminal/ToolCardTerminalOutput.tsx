/**
 * Read-only terminal renderer for chat tool cards.
 *
 * The component lazily mounts xterm only when the card is expanded and output
 * is ready, then disposes terminal resources on unmount.
 */
import { useEffect, useRef } from "react";

import { Terminal as XTerm } from "xterm";
import { FitAddon } from "xterm-addon-fit";
import "xterm/css/xterm.css";

/** ANSI dim/misty color for command output (slate-500-ish). Command line stays default. */
const ANSI_DIM_OUTPUT = "\x1b[38;5;246m";
const ANSI_RESET = "\x1b[0m";

function writeWithDimOutput(terminal: XTerm, outputText: string): void {
  const firstNewline = outputText.indexOf("\n");
  if (firstNewline === -1) {
    terminal.write(outputText);
    return;
  }
  const commandLine = outputText.slice(0, firstNewline);
  const outputBody = outputText.slice(firstNewline + 1);
  terminal.write(commandLine + "\n" + ANSI_DIM_OUTPUT + outputBody + ANSI_RESET);
}

function fitTerminalToContainer(fitAddon: FitAddon | null): void {
  try {
    fitAddon?.fit();
  } catch {
    // xterm can throw while an expanding card is still zero-sized; resize will retry.
  }
}

interface ToolCardTerminalOutputProps {
  outputText: string;
  isExpanded: boolean;
  isReady: boolean;
  testId?: string;
}

export function ToolCardTerminalOutput({
  outputText,
  isExpanded,
  isReady,
  testId,
}: ToolCardTerminalOutputProps) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const terminalRef = useRef<XTerm | null>(null);
  const fitAddonRef = useRef<FitAddon | null>(null);

  const shouldMountTerminal = isExpanded && isReady;

  useEffect(() => {
    if (!shouldMountTerminal || !containerRef.current) {
      return;
    }

    const container = containerRef.current;
    const terminal = new XTerm({
      disableStdin: true,
      convertEol: true,
      cursorBlink: false,
      cursorStyle: "bar",
      scrollback: 4000,
      fontFamily: "ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace",
      fontSize: 12,
      lineHeight: 1.4,
      theme: {
        background: "#020617",
        foreground: "#cbd5e1",
      },
    });

    const fitAddon = new FitAddon();
    terminal.loadAddon(fitAddon);
    terminal.open(container);
    fitTerminalToContainer(fitAddon);

    terminalRef.current = terminal;
    fitAddonRef.current = fitAddon;

    let animationFrameId: number | null = null;
    const scheduleFit = () => {
      if (typeof window.requestAnimationFrame !== "function") {
        fitTerminalToContainer(fitAddon);
        return;
      }
      if (animationFrameId !== null) {
        window.cancelAnimationFrame(animationFrameId);
      }
      animationFrameId = window.requestAnimationFrame(() => {
        animationFrameId = null;
        fitTerminalToContainer(fitAddon);
      });
    };

    scheduleFit();
    const resizeObserver =
      typeof ResizeObserver === "undefined"
        ? null
        : new ResizeObserver(() => {
            scheduleFit();
          });
    resizeObserver?.observe(container);

    return () => {
      resizeObserver?.disconnect();
      if (animationFrameId !== null && typeof window.cancelAnimationFrame === "function") {
        window.cancelAnimationFrame(animationFrameId);
      }
      terminal.dispose();
      terminalRef.current = null;
      fitAddonRef.current = null;
    };
  }, [shouldMountTerminal]);

  useEffect(() => {
    if (!shouldMountTerminal || !terminalRef.current) {
      return;
    }
    terminalRef.current.clear();
    writeWithDimOutput(terminalRef.current, outputText);
    fitTerminalToContainer(fitAddonRef.current);
  }, [outputText, shouldMountTerminal]);

  if (!shouldMountTerminal) {
    return null;
  }

  return (
    <div
      data-testid={testId}
      className="h-56 w-full min-w-0 max-w-full overflow-hidden rounded border border-slate-800/70 bg-slate-950/80"
    >
      <div ref={containerRef} className="h-full w-full min-w-0" />
    </div>
  );
}
