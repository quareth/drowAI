/**
 * Streaming JSON Parser
 * 
 * Incrementally parses JSON content as it streams in, providing partial
 * parsing results and visual rendering of incomplete JSON structures.
 * Similar to how ChatGPT renders JSON in real-time.
 */

export type JsonTokenType = 
  | 'brace-open' 
  | 'brace-close' 
  | 'bracket-open' 
  | 'bracket-close'
  | 'colon'
  | 'comma'
  | 'string'
  | 'number'
  | 'boolean'
  | 'null'
  | 'whitespace'
  | 'key'
  | 'incomplete-string'
  | 'incomplete-key';

export interface JsonToken {
  type: JsonTokenType;
  value: string;
  depth: number;
}

export interface StreamingJsonState {
  tokens: JsonToken[];
  depth: number;
  inString: boolean;
  inKey: boolean;
  isComplete: boolean;
  isValid: boolean;
  hasStarted: boolean;
  partialToken: string;
}

/**
 * Detects if content looks like it's starting to be JSON
 */
export function looksLikeJson(content: string): boolean {
  const trimmed = content.trimStart();
  return trimmed.startsWith('{') || trimmed.startsWith('[');
}

/**
 * Check if the content is complete, valid JSON
 */
export function isCompleteJson(content: string): boolean {
  try {
    JSON.parse(content);
    return true;
  } catch {
    return false;
  }
}

/**
 * Find the end index of a JSON object/array by counting brackets.
 * Returns the position AFTER the closing bracket, or -1 if incomplete.
 */
export function findJsonEnd(content: string): number {
  let depth = 0;
  let inString = false;
  let started = false;
  
  for (let i = 0; i < content.length; i++) {
    const char = content[i];
    const prevChar = i > 0 ? content[i - 1] : '';
    
    // Handle string escapes
    if (inString) {
      if (char === '"' && prevChar !== '\\') {
        inString = false;
      }
      continue;
    }
    
    if (char === '"') {
      inString = true;
      continue;
    }
    
    if (char === '{' || char === '[') {
      started = true;
      depth++;
    } else if (char === '}' || char === ']') {
      depth--;
      if (started && depth === 0) {
        return i + 1; // Return position after closing bracket
      }
    }
  }
  
  return -1; // JSON not complete
}

/**
 * Parse streaming JSON content into tokens for rendering
 * Handles incomplete JSON gracefully
 */
export function parseStreamingJson(content: string): StreamingJsonState {
  const tokens: JsonToken[] = [];
  let depth = 0;
  let inString = false;
  let inKey = false;
  let i = 0;
  let partialToken = '';
  let hasStarted = false;
  
  const trimmed = content.trimStart();
  const leadingWhitespace = content.length - trimmed.length;
  
  // Add leading whitespace if any
  if (leadingWhitespace > 0) {
    tokens.push({ type: 'whitespace', value: content.slice(0, leadingWhitespace), depth: 0 });
  }
  
  i = leadingWhitespace;
  
  while (i < content.length) {
    const char = content[i];
    
    // Handle escape sequences in strings
    if (inString && char === '\\' && i + 1 < content.length) {
      partialToken += char + content[i + 1];
      i += 2;
      continue;
    }
    
    // Handle string content
    if (inString) {
      if (char === '"') {
        partialToken += char;
        tokens.push({ 
          type: inKey ? 'key' : 'string', 
          value: partialToken, 
          depth 
        });
        partialToken = '';
        inString = false;
        inKey = false;
        i++;
        continue;
      }
      partialToken += char;
      i++;
      continue;
    }
    
    // Skip whitespace outside strings
    if (/\s/.test(char)) {
      i++;
      continue;
    }
    
    // Handle structural characters
    switch (char) {
      case '{':
        hasStarted = true;
        tokens.push({ type: 'brace-open', value: '{', depth });
        depth++;
        inKey = true; // Next string after { is a key
        i++;
        break;
        
      case '}':
        depth = Math.max(0, depth - 1);
        tokens.push({ type: 'brace-close', value: '}', depth });
        i++;
        break;
        
      case '[':
        hasStarted = true;
        tokens.push({ type: 'bracket-open', value: '[', depth });
        depth++;
        i++;
        break;
        
      case ']':
        depth = Math.max(0, depth - 1);
        tokens.push({ type: 'bracket-close', value: ']', depth });
        i++;
        break;
        
      case ':':
        tokens.push({ type: 'colon', value: ':', depth });
        inKey = false;
        i++;
        break;
        
      case ',':
        tokens.push({ type: 'comma', value: ',', depth });
        // After comma in an object, next string is a key
        // This is a simplification - proper tracking would need stack
        inKey = true;
        i++;
        break;
        
      case '"':
        inString = true;
        partialToken = '"';
        i++;
        break;
        
      default: {
        // Handle literals: true, false, null, numbers
        const remaining = content.slice(i);
        
        if (remaining.startsWith('true')) {
          tokens.push({ type: 'boolean', value: 'true', depth });
          i += 4;
        } else if (remaining.startsWith('false')) {
          tokens.push({ type: 'boolean', value: 'false', depth });
          i += 5;
        } else if (remaining.startsWith('null')) {
          tokens.push({ type: 'null', value: 'null', depth });
          i += 4;
        } else if (/^-?\d/.test(remaining)) {
          // Parse number
          const numMatch = remaining.match(/^-?\d+\.?\d*(?:[eE][+-]?\d+)?/);
          if (numMatch) {
            tokens.push({ type: 'number', value: numMatch[0], depth });
            i += numMatch[0].length;
          } else {
            // Incomplete number at end of stream
            partialToken = remaining;
            i = content.length;
          }
        } else {
          // Unknown character, skip
          i++;
        }
        break;
      }
    }
  }
  
  // Handle incomplete string at end
  if (inString && partialToken) {
    tokens.push({ 
      type: inKey ? 'incomplete-key' : 'incomplete-string', 
      value: partialToken, 
      depth 
    });
  }
  
  const isComplete = depth === 0 && !inString && hasStarted;
  const isValid = isComplete && isCompleteJson(content);
  
  return {
    tokens,
    depth,
    inString,
    inKey,
    isComplete,
    isValid,
    hasStarted,
    partialToken: inString ? partialToken : '',
  };
}

/**
 * Format JSON tokens into indented lines for display
 */
export interface FormattedJsonLine {
  indent: number;
  tokens: JsonToken[];
}

export function formatStreamingJson(state: StreamingJsonState): FormattedJsonLine[] {
  const lines: FormattedJsonLine[] = [];
  let currentLine: JsonToken[] = [];
  let currentIndent = 0;
  
  for (const token of state.tokens) {
    if (token.type === 'whitespace') {
      continue;
    }
    
    if (token.type === 'brace-open' || token.type === 'bracket-open') {
      currentLine.push(token);
      lines.push({ indent: currentIndent, tokens: [...currentLine] });
      currentLine = [];
      currentIndent = token.depth + 1;
    } else if (token.type === 'brace-close' || token.type === 'bracket-close') {
      if (currentLine.length > 0) {
        lines.push({ indent: currentIndent, tokens: [...currentLine] });
        currentLine = [];
      }
      currentIndent = token.depth;
      currentLine.push(token);
      lines.push({ indent: currentIndent, tokens: [...currentLine] });
      currentLine = [];
    } else if (token.type === 'comma') {
      currentLine.push(token);
      lines.push({ indent: currentIndent, tokens: [...currentLine] });
      currentLine = [];
    } else {
      currentLine.push(token);
    }
  }
  
  // Push any remaining tokens
  if (currentLine.length > 0) {
    lines.push({ indent: currentIndent, tokens: currentLine });
  }
  
  return lines;
}

