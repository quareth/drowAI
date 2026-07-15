/**
 * StreamingContent
 * 
 * Renders streaming content like ChatGPT - text streams normally,
 * and when JSON appears, a container appears inline and JSON streams into it.
 * Handles mixed content: text → JSON → text seamlessly.
 */

import React, { useMemo } from 'react';
import { MarkdownMessage } from './markdown-message';
import { StreamingJsonViewer } from './streaming-json-viewer';
import { JsonViewer, tryParseJson } from './json-viewer';
import { findJsonEnd } from '@/utils/streaming-json-parser';

interface ContentSegment {
  type: 'text' | 'json';
  content: string;
  isComplete: boolean;
}

interface StreamingContentProps {
  content: string;
  isStreaming?: boolean;
}

/**
 * Parse content into segments of text and JSON
 * Detects both ```json code blocks and inline JSON objects
 */
function parseContentSegments(content: string, isStreaming: boolean): ContentSegment[] {
  const segments: ContentSegment[] = [];
  let remaining = content;
  
  while (remaining.length > 0) {
    // Check for ```json code block
    const codeBlockMatch = remaining.match(/```json\s*/i);
    
    // Check for inline JSON (newline followed by { or [)
    const inlineJsonMatch = remaining.match(/\n\s*([{\[])/);
    
    // Also check if content starts with JSON
    const startsWithJson = /^\s*[{\[]/.test(remaining);
    
    // Determine which comes first
    let jsonStartIndex = -1;
    let jsonStartOffset = 0; // How many chars to skip to get to actual JSON
    let isCodeBlock = false;
    
    if (codeBlockMatch?.index !== undefined) {
      jsonStartIndex = codeBlockMatch.index;
      jsonStartOffset = codeBlockMatch[0].length;
      isCodeBlock = true;
    }
    
    if (inlineJsonMatch?.index !== undefined) {
      if (jsonStartIndex === -1 || inlineJsonMatch.index < jsonStartIndex) {
        jsonStartIndex = inlineJsonMatch.index;
        jsonStartOffset = inlineJsonMatch[0].length - 1; // Position of { or [
        isCodeBlock = false;
      }
    }
    
    if (startsWithJson && jsonStartIndex !== 0) {
      jsonStartIndex = 0;
      jsonStartOffset = remaining.match(/^\s*/)?.[0].length ?? 0;
      isCodeBlock = false;
    }
    
    // No JSON found - rest is text
    if (jsonStartIndex === -1) {
      if (remaining.trim()) {
        segments.push({ type: 'text', content: remaining, isComplete: !isStreaming });
      }
      break;
    }
    
    // Add text before JSON
    const textBefore = remaining.slice(0, jsonStartIndex);
    if (textBefore.trim()) {
      segments.push({ type: 'text', content: textBefore, isComplete: true });
    }
    
    // Extract JSON content
    const jsonStart = jsonStartIndex + jsonStartOffset;
    const afterJsonStart = remaining.slice(jsonStart);
    
    // Find end of JSON
    let jsonEnd = -1;
    let jsonComplete = false;
    
    if (isCodeBlock) {
      // Look for closing ```
      const closingIndex = afterJsonStart.indexOf('```');
      if (closingIndex !== -1) {
        jsonEnd = jsonStart + closingIndex;
        jsonComplete = true;
      }
    } else {
      // For inline JSON, try to find the balanced closing bracket
      jsonEnd = findJsonEnd(afterJsonStart);
      if (jsonEnd !== -1) {
        jsonEnd = jsonStart + jsonEnd;
        jsonComplete = true;
      }
    }
    
    if (jsonEnd === -1) {
      // JSON is still streaming - take rest of content
      const jsonContent = afterJsonStart.trim();
      if (jsonContent) {
        segments.push({ type: 'json', content: jsonContent, isComplete: false });
      }
      break;
    }
    
    // Complete JSON segment
    const jsonContent = remaining.slice(jsonStart, jsonEnd).trim();
    if (jsonContent) {
      segments.push({ type: 'json', content: jsonContent, isComplete: jsonComplete });
    }
    
    // Continue with remaining content (skip closing ``` if code block)
    remaining = remaining.slice(jsonEnd + (isCodeBlock ? 3 : 0));
  }
  
  return segments;
}

/**
 * Renders a single segment
 */
function SegmentRenderer({ segment, isStreaming }: { segment: ContentSegment; isStreaming: boolean }) {
  if (segment.type === 'text') {
    return <MarkdownMessage content={segment.content} />;
  }
  
  // JSON segment - use interactive viewer if complete, streaming viewer if not
  if (segment.isComplete) {
    const parsed = tryParseJson(segment.content);
    if (parsed !== null) {
      return <JsonViewer data={parsed} initialExpanded={true} />;
    }
  }
  
  // Streaming or incomplete JSON
  return (
    <StreamingJsonViewer 
      content={segment.content} 
      isStreaming={isStreaming || !segment.isComplete} 
    />
  );
}

/**
 * Main component - renders content with inline JSON containers
 */
export function StreamingContent({ content, isStreaming = false }: StreamingContentProps) {
  const segments = useMemo(
    () => parseContentSegments(content, isStreaming),
    [content, isStreaming]
  );
  
  if (segments.length === 0) {
    return null;
  }
  
  // Single segment - render directly
  if (segments.length === 1) {
    return <SegmentRenderer segment={segments[0]} isStreaming={isStreaming} />;
  }
  
  // Multiple segments - render with spacing
  return (
    <div className="space-y-3">
      {segments.map((segment, index) => (
        <SegmentRenderer 
          key={`${segment.type}-${index}`} 
          segment={segment} 
          isStreaming={isStreaming && index === segments.length - 1} 
        />
      ))}
    </div>
  );
}

export default StreamingContent;
