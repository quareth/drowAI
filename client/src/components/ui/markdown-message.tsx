import React from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { JsonViewer, tryParseJson } from './json-viewer';

type MarkdownMessageProps = {
	content: string;
};

/**
 * Reusable markdown renderer for agent/user messages.
 * - Renders GitHub-flavored markdown (tables, fenced code, inline code)
 * - Uses Tailwind Typography for pleasant defaults on dark backgrounds
 * - Styles code blocks with rounded container and subtle border
 * - JSON code blocks are rendered with interactive JsonViewer
 * 
 * Note: Streaming JSON is handled by StreamingContent, not here.
 */
export function MarkdownMessage({ content }: MarkdownMessageProps) {
    return (
        <div className="prose prose-invert max-w-none text-sm leading-6 prose-p:my-2 prose-ul:my-2 prose-ol:my-2 prose-strong:text-gray-100 prose-code:font-mono prose-code:text-[12.5px] selection:bg-slate-700/60">
            <ReactMarkdown
                remarkPlugins={[remarkGfm]}
                components={{
                    code({ className, children }) {
                        const language = /language-(\w+)/.exec(className || '')?.[1];

                        const rawCodeText = String(children ?? '');
                        const normalizedCodeText = rawCodeText.replace(/\n$/, '');
                        const inline = !className && !normalizedCodeText.includes('\n');
                        const isSingleLineCodeBlock =
                            !inline && !language && !normalizedCodeText.includes('\n');

                        if (isSingleLineCodeBlock) {
                            return (
                                <code
                                    className="font-mono text-[12.5px] text-slate-100 bg-transparent border-0 p-0"
                                >
                                    {normalizedCodeText}
                                </code>
                            );
                        }

                        // Render complete JSON code blocks with interactive JsonViewer
                        if (!inline && language === 'json') {
                            const trimmed = normalizedCodeText.trim();
                            const parsed = tryParseJson(trimmed);
                            if (parsed !== null) {
                                return <JsonViewer data={parsed} initialExpanded={true} />;
                            }
                            // Incomplete JSON falls through to regular code block rendering
                        }

                        if (!inline) {
                            return (
                                <pre className="overflow-x-auto rounded-md border border-slate-800 bg-slate-900 p-3">
                                    <code className={(className || (language ? `language-${language}` : '')) + ' !bg-transparent !border-0'}>
                                        {children}
                                    </code>
                                </pre>
                            );
                        }
                        return (
                            <code
                                className="font-mono text-[12.5px] text-slate-100 bg-transparent border-0 p-0"
                            >
                                {children}
                            </code>
                        );
                    },
                }}
            >
                {content}
            </ReactMarkdown>
        </div>
    );
}

export default MarkdownMessage;

