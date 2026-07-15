/**
 * Clarify-required interrupt card for collecting mandatory blocker answers.
 */
import { useMemo, useState } from "react";

import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import type { ClarifyRequestPayload } from "@/types/hitl";

interface ClarifyRequiredCardProps {
  payload: ClarifyRequestPayload;
  onSubmit: (answers: Record<string, string>, note?: string) => void;
  isSubmitting?: boolean;
}

export function ClarifyRequiredCard({
  payload,
  onSubmit,
  isSubmitting = false,
}: ClarifyRequiredCardProps) {
  const initialAnswers = useMemo(() => {
    const values: Record<string, string> = {};
    for (const question of payload.questions) {
      if (Array.isArray(question.options) && question.options.length > 0) {
        values[question.question_id] = question.options[0];
      } else {
        values[question.question_id] = "";
      }
    }
    return values;
  }, [payload.questions]);

  const [answers, setAnswers] = useState<Record<string, string>>(initialAnswers);
  const [errors, setErrors] = useState<Record<string, string>>({});

  const handleChange = (questionId: string, value: string) => {
    setAnswers((prev) => ({ ...prev, [questionId]: value }));
    setErrors((prev) => {
      if (!prev[questionId]) return prev;
      const next = { ...prev };
      delete next[questionId];
      return next;
    });
  };

  const handleSubmit = () => {
    const nextErrors: Record<string, string> = {};
    for (const question of payload.questions) {
      const required = question.required !== false;
      const value = answers[question.question_id] ?? "";
      if (required && !String(value).trim()) {
        nextErrors[question.question_id] = "This field is required.";
        continue;
      }
      if (
        required &&
        Array.isArray(question.options) &&
        question.options.length > 0 &&
        !question.options.includes(String(value))
      ) {
        nextErrors[question.question_id] = "Choose one of the provided options.";
      }
    }
    setErrors(nextErrors);
    if (Object.keys(nextErrors).length > 0) return;
    onSubmit(answers);
  };

  return (
    <div className="inline-block rounded-md border border-slate-700/40 bg-slate-900/50 p-3 shadow-lg shadow-black/20 backdrop-blur-sm">
      <p className="mb-2 text-xs font-semibold text-slate-200">Clarification Required</p>
      <div className="space-y-3">
        {payload.questions.map((question) => {
          const value = answers[question.question_id] ?? "";
          const required = question.required !== false;
          const error = errors[question.question_id];

          return (
            <div key={question.question_id} className="space-y-1">
              <label className="block text-xs text-slate-300">
                {question.label}
                {required ? <span className="ml-1 text-rose-400">*</span> : null}
              </label>
              <Select
                value={value}
                onValueChange={(v) => handleChange(question.question_id, v)}
                disabled={isSubmitting}
              >
                <SelectTrigger aria-label={question.label} className="h-8 w-72 text-xs">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {(question.options ?? []).map((option) => (
                    <SelectItem key={option} value={option} className="text-xs">
                      {option}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
              {error ? <p className="text-[11px] text-rose-400">{error}</p> : null}
            </div>
          );
        })}
      </div>
      <div className="mt-3 flex items-center justify-end">
        <button
          type="button"
          onClick={handleSubmit}
          disabled={isSubmitting}
          className="rounded bg-emerald-600 px-3 py-1 text-xs font-semibold text-white transition-colors hover:bg-emerald-500 disabled:opacity-50"
        >
          Submit Answers
        </button>
      </div>
    </div>
  );
}

export default ClarifyRequiredCard;
