import { useState, useMemo, useCallback } from "react";
import { ChevronDown, ChevronRight, Copy, Check } from "lucide-react";
import { cn } from "@/lib/utils";

interface JsonViewerProps {
  data: unknown;
  initialExpanded?: boolean;
  className?: string;
}

type JsonValueType = "string" | "number" | "boolean" | "null" | "object" | "array";

function getValueType(value: unknown): JsonValueType {
  if (value === null) return "null";
  if (Array.isArray(value)) return "array";
  return typeof value as JsonValueType;
}

const VALUE_COLORS: Record<JsonValueType, string> = {
  string: "text-emerald-400",
  number: "text-amber-400",
  boolean: "text-purple-400",
  null: "text-slate-500",
  object: "text-slate-300",
  array: "text-slate-300",
};

const KEY_COLOR = "text-cyan-400";
const BRACKET_COLOR = "text-slate-400";
const PUNCTUATION_COLOR = "text-slate-500";

interface JsonNodeProps {
  keyName?: string;
  value: unknown;
  depth: number;
  isLast: boolean;
  defaultExpanded: boolean;
}

function JsonNode({ keyName, value, depth, isLast, defaultExpanded }: JsonNodeProps) {
  const valueType = getValueType(value);
  const isExpandable = valueType === "object" || valueType === "array";
  const [expanded, setExpanded] = useState(defaultExpanded && depth < 2);

  const indent = depth * 16;

  const toggleExpand = useCallback(() => {
    if (isExpandable) {
      setExpanded((prev) => !prev);
    }
  }, [isExpandable]);

  // Compute these values for expandable types (needed for hooks to be called consistently)
  const isArray = isExpandable && valueType === "array";
  const entries = useMemo(() => {
    if (!isExpandable) return [];
    if (isArray) {
      return (value as unknown[]).map((v, i) => [String(i), v] as const);
    }
    return Object.entries(value as Record<string, unknown>);
  }, [value, isExpandable, isArray]);
  const isEmpty = entries.length === 0;

  // Collapsed preview - must be called before any conditional returns (React hooks rule)
  const collapsedPreview = useMemo(() => {
    if (!isExpandable || isEmpty) return "";
    if (isArray) {
      const arr = value as unknown[];
      if (arr.length <= 3 && arr.every((v) => typeof v !== "object" || v === null)) {
        return arr.map((v) => (typeof v === "string" ? `"${v}"` : String(v))).join(", ");
      }
      return `${arr.length} items`;
    }
    const keys = Object.keys(value as object);
    if (keys.length <= 3) {
      return keys.join(", ");
    }
    return `${keys.length} keys`;
  }, [value, isEmpty, isArray, isExpandable]);

  // Render primitive values (after all hooks)
  if (!isExpandable) {
    let displayValue: string;
    if (valueType === "string") {
      displayValue = `"${value}"`;
    } else if (valueType === "null") {
      displayValue = "null";
    } else {
      displayValue = String(value);
    }

    return (
      <div className="flex items-start" style={{ paddingLeft: indent }}>
        {keyName !== undefined && (
          <>
            <span className={KEY_COLOR}>"{keyName}"</span>
            <span className={PUNCTUATION_COLOR}>: </span>
          </>
        )}
        <span className={VALUE_COLORS[valueType]}>{displayValue}</span>
        {!isLast && <span className={PUNCTUATION_COLOR}>,</span>}
      </div>
    );
  }

  // Render objects and arrays
  const openBracket = isArray ? "[" : "{";
  const closeBracket = isArray ? "]" : "}";

  return (
    <div>
      <div
        className={cn("flex items-start", isExpandable && "cursor-pointer hover:bg-slate-800/50 rounded")}
        style={{ paddingLeft: indent }}
        onClick={toggleExpand}
      >
        {isExpandable && (
          <span className="w-4 h-4 flex items-center justify-center mr-1 text-slate-500">
            {expanded ? (
              <ChevronDown className="w-3 h-3" />
            ) : (
              <ChevronRight className="w-3 h-3" />
            )}
          </span>
        )}
        {keyName !== undefined && (
          <>
            <span className={KEY_COLOR}>"{keyName}"</span>
            <span className={PUNCTUATION_COLOR}>: </span>
          </>
        )}
        <span className={BRACKET_COLOR}>{openBracket}</span>
        {!expanded && !isEmpty && (
          <>
            <span className="text-slate-500 text-xs ml-1 italic">{collapsedPreview}</span>
            <span className={BRACKET_COLOR}>{closeBracket}</span>
            {!isLast && <span className={PUNCTUATION_COLOR}>,</span>}
          </>
        )}
        {isEmpty && (
          <>
            <span className={BRACKET_COLOR}>{closeBracket}</span>
            {!isLast && <span className={PUNCTUATION_COLOR}>,</span>}
          </>
        )}
      </div>
      {expanded && !isEmpty && (
        <>
          {entries.map(([k, v], i) => (
            <JsonNode
              key={k}
              keyName={isArray ? undefined : k}
              value={v}
              depth={depth + 1}
              isLast={i === entries.length - 1}
              defaultExpanded={defaultExpanded}
            />
          ))}
          <div style={{ paddingLeft: indent }}>
            <span className={BRACKET_COLOR}>{closeBracket}</span>
            {!isLast && <span className={PUNCTUATION_COLOR}>,</span>}
          </div>
        </>
      )}
    </div>
  );
}

export function JsonViewer({ data, initialExpanded = true, className }: JsonViewerProps) {
  const [copied, setCopied] = useState(false);

  const jsonString = useMemo(() => {
    try {
      return JSON.stringify(data, null, 2);
    } catch {
      return String(data);
    }
  }, [data]);

  const handleCopy = useCallback(async () => {
    try {
      await navigator.clipboard.writeText(jsonString);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch (err) {
      console.error("Failed to copy JSON:", err);
    }
  }, [jsonString]);

  return (
    <div
      className={cn(
        "relative rounded-lg border border-slate-700 bg-slate-900/80 overflow-hidden",
        className
      )}
    >
      {/* Header */}
      <div className="flex items-center justify-between px-3 py-2 border-b border-slate-700 bg-slate-800/50">
        <span className="text-xs font-medium text-slate-400 uppercase tracking-wide">JSON</span>
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
      <div className="p-3 overflow-x-auto font-mono text-xs leading-5">
        <JsonNode value={data} depth={0} isLast={true} defaultExpanded={initialExpanded} />
      </div>
    </div>
  );
}

/**
 * Attempts to parse a string as JSON.
 * Returns the parsed object if successful, null otherwise.
 */
export function tryParseJson(content: string): unknown | null {
  const trimmed = content.trim();
  // Quick check: must start with { or [
  if (!trimmed.startsWith("{") && !trimmed.startsWith("[")) {
    return null;
  }
  try {
    return JSON.parse(trimmed);
  } catch {
    return null;
  }
}

export default JsonViewer;

