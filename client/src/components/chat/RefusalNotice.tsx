/**
 * Provider-neutral refusal notice rendered as escaped plain text.
 *
 * Provider explanations intentionally bypass Markdown and HTML renderers so
 * untrusted provider text cannot create interactive links or markup.
 */

import { ChevronDown, ShieldAlert } from "lucide-react";

import type { ProviderRefusalMetadata } from "./types";

interface RefusalNoticeProps {
  refusal?: ProviderRefusalMetadata;
  fallbackSummary: string;
}

interface DetailRowProps {
  label: string;
  value?: string | null;
}

function DetailRow({ label, value }: DetailRowProps) {
  if (!value) return null;
  return (
    <div className="grid gap-1 sm:grid-cols-[7rem_1fr]">
      <dt className="font-medium text-amber-300/80">{label}</dt>
      <dd className="min-w-0 whitespace-pre-wrap break-words text-amber-50/90">{value}</dd>
    </div>
  );
}

export function RefusalNotice({ refusal, fallbackSummary }: RefusalNoticeProps) {
  const model = refusal?.model?.trim();
  const title = model ? `${model} declined this request` : "Provider declined this request";
  const summary = refusal?.summary?.trim() || fallbackSummary;
  const hasDetails = Boolean(
    refusal &&
      (refusal.provider ||
        refusal.model ||
        refusal.category ||
        refusal.explanation ||
        refusal.response_id),
  );

  return (
    <section
      aria-label="Provider refusal"
      className="rounded-xl border border-amber-500/40 bg-amber-500/10 p-3 text-amber-50"
    >
      <div className="flex items-start gap-2.5">
        <ShieldAlert className="mt-0.5 h-4 w-4 shrink-0 text-amber-300" aria-hidden="true" />
        <div className="min-w-0 space-y-1">
          <h3 className="text-sm font-semibold text-amber-100">{title}</h3>
          <p className="whitespace-pre-wrap text-sm leading-relaxed text-amber-50/90">
            {summary}
          </p>
        </div>
      </div>

      {hasDetails && (
        <details className="group mt-3 border-t border-amber-500/20 pt-2">
          <summary className="flex cursor-pointer list-none items-center gap-1.5 text-xs font-medium text-amber-200 hover:text-amber-100">
            <ChevronDown
              className="h-3.5 w-3.5 transition-transform group-open:rotate-180"
              aria-hidden="true"
            />
            Provider details
          </summary>
          <dl className="mt-2 space-y-2 text-xs">
            <DetailRow label="Provider" value={refusal?.provider} />
            <DetailRow label="Model" value={refusal?.model} />
            <DetailRow label="Category" value={refusal?.category} />
            <DetailRow label="Explanation" value={refusal?.explanation} />
            <DetailRow label="Response ID" value={refusal?.response_id} />
          </dl>
        </details>
      )}
    </section>
  );
}

export default RefusalNotice;
