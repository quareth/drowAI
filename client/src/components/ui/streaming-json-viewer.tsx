/**
 * StreamingJsonViewer
 * 
 * Renders JSON content in real-time as it streams in, similar to ChatGPT's
 * JSON rendering. Shows partial JSON with syntax highlighting and visual
 * indicators for incomplete content.
 */

import { useMemo, useState, useCallback } from "react";
import { Copy, Check, Loader2 } from "lucide-react";
import { cn } from "@/lib/utils";
import {
  parseStreamingJson,
  formatStreamingJson,
  type JsonToken,
  type FormattedJsonLine,
} from "@/utils/streaming-json-parser";

// Token color classes (matching json-viewer.tsx aesthetic)
const TOKEN_COLORS: Record<string, string> = {
  'brace-open': 'text-slate-400',
  'brace-close': 'text-slate-400',
  'bracket-open': 'text-slate-400',
  'bracket-close': 'text-slate-400',
  'colon': 'text-slate-500',
  'comma': 'text-slate-500',
  'string': 'text-emerald-400',
  'number': 'text-amber-400',
  'boolean': 'text-purple-400',
  'null': 'text-slate-500',
  'key': 'text-cyan-400',
  'incomplete-string': 'text-emerald-400/70',
  'incomplete-key': 'text-cyan-400/70',
};

interface StreamingJsonViewerProps {
  content: string;
  isStreaming?: boolean;
  className?: string;
}

function TokenRenderer({ token }: { token: JsonToken }) {
  const colorClass = TOKEN_COLORS[token.type] ?? 'text-slate-300';
  const isIncomplete = token.type.startsWith('incomplete-');
  
  return (
    <span 
      className={cn(
        colorClass,
        isIncomplete && 'animate-pulse'
      )}
    >
      {token.value}
    </span>
  );
}

function LineRenderer({ line, isLast, isStreaming }: { 
  line: FormattedJsonLine; 
  isLast: boolean;
  isStreaming: boolean;
}) {
  const indent = line.indent * 16; // 16px per indent level
  const hasIncomplete = line.tokens.some(t => t.type.startsWith('incomplete-'));
  
  return (
    <div 
      className="flex items-start min-h-[20px]"
      style={{ paddingLeft: `${indent}px` }}
    >
      {line.tokens.map((token, i) => (
        <TokenRenderer key={`${i}-${token.value}`} token={token} />
      ))}
      {isLast && isStreaming && !hasIncomplete && (
        <span className="inline-block w-[2px] h-4 bg-slate-400 animate-pulse ml-0.5" />
      )}
    </div>
  );
}

export function StreamingJsonViewer({ 
  content, 
  isStreaming = false,
  className 
}: StreamingJsonViewerProps) {
  const [copied, setCopied] = useState(false);
  
  // Parse and format the streaming JSON
  const { lines, state } = useMemo(() => {
    // Handle empty or whitespace-only content
    const trimmed = (content || '').trim();
    if (!trimmed) {
      return { 
        lines: [] as FormattedJsonLine[], 
        state: { 
          tokens: [], 
          depth: 0, 
          inString: false, 
          inKey: false, 
          isComplete: false, 
          isValid: false, 
          hasStarted: false,
          partialToken: '' 
        } 
      };
    }
    const parsed = parseStreamingJson(trimmed);
    const formatted = formatStreamingJson(parsed);
    return { lines: formatted, state: parsed };
  }, [content]);
  
  const handleCopy = useCallback(async () => {
    try {
      await navigator.clipboard.writeText(content);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch (err) {
      console.error("Failed to copy JSON:", err);
    }
  }, [content]);
  
  // Show a streaming indicator in header when actively streaming
  const showStreamingBadge = isStreaming && !state.isComplete;
  
  return (
    <div
      className={cn(
        "relative rounded-lg border border-slate-700 bg-slate-900/80 overflow-hidden",
        isStreaming && "border-cyan-700/50",
        className
      )}
    >
      {/* Header */}
      <div className="flex items-center justify-between px-3 py-2 border-b border-slate-700 bg-slate-800/50">
        <div className="flex items-center gap-2">
          <span className="text-xs font-medium text-slate-400 uppercase tracking-wide">
            JSON
          </span>
          {showStreamingBadge && (
            <span className="flex items-center gap-1 text-[10px] text-cyan-400 uppercase tracking-wide">
              <Loader2 className="w-3 h-3 animate-spin" />
              Streaming
            </span>
          )}
        </div>
        <button
          onClick={handleCopy}
          className="flex items-center gap-1 text-xs text-slate-400 hover:text-slate-200 transition-colors"
          title="Copy JSON"
        >
          {copied ? (
            <>
              <Check className="w-3.5 h-3.5 text-emerald-400" />
              <span className="text-emerald-400">Copied</span>
            </>
          ) : (
            <>
              <Copy className="w-3.5 h-3.5" />
              <span>Copy</span>
            </>
          )}
        </button>
      </div>
      
      {/* Content */}
      <div className="p-3 overflow-x-auto font-mono text-xs leading-5 max-h-[400px] overflow-y-auto">
        {lines.length === 0 && isStreaming ? (
          <div className="text-slate-500 italic flex items-center gap-2">
            <Loader2 className="w-3 h-3 animate-spin" />
            Receiving JSON...
          </div>
        ) : (
          lines.map((line, i) => (
            <LineRenderer 
              key={i} 
              line={line} 
              isLast={i === lines.length - 1}
              isStreaming={isStreaming}
            />
          ))
        )}
      </div>
      
      {/* Bottom streaming indicator bar */}
      {isStreaming && !state.isComplete && (
        <div className="h-0.5 bg-gradient-to-r from-cyan-500/50 via-cyan-400/80 to-cyan-500/50 animate-pulse" />
      )}
    </div>
  );
}

export default StreamingJsonViewer;

