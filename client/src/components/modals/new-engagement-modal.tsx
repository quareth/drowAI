/**
 * Standalone dialog to create an engagement (name + description) via POST /api/engagements/.
 */

import { useEffect } from "react";
import { useForm } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import { z } from "zod";

import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { useToast } from "@/hooks/use-toast";
import { useCreateEngagement } from "@/hooks/use-engagement-knowledge";

const schema = z.object({
  name: z.string().min(1, "Name is required"),
  description: z.string().optional(),
});

type FormValues = z.infer<typeof schema>;

export interface NewEngagementModalProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
}

export function NewEngagementModal({ open, onOpenChange }: NewEngagementModalProps) {
  const { toast } = useToast();
  const createEngagement = useCreateEngagement();

  const form = useForm<FormValues>({
    resolver: zodResolver(schema),
    defaultValues: { name: "", description: "" },
  });

  useEffect(() => {
    if (!open) {
      form.reset({ name: "", description: "" });
    }
  }, [open, form]);

  const onSubmit = async (values: FormValues) => {
    try {
      const data = await createEngagement.mutateAsync({
        name: values.name.trim(),
        description: values.description?.trim() || undefined,
      });
      toast({ title: "Engagement created", description: data.name });
      onOpenChange(false);
      form.reset();
    } catch (err) {
      toast({
        title: "Failed to create engagement",
        description: err instanceof Error ? err.message : "Unknown error",
        variant: "destructive",
      });
    }
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-md border-slate-700 bg-slate-900">
        <DialogHeader>
          <DialogTitle className="text-lg font-semibold text-white">New engagement</DialogTitle>
        </DialogHeader>
        <form onSubmit={form.handleSubmit(onSubmit)} className="space-y-4">
          <div>
            <Label htmlFor="eng-name" className="text-slate-300">
              Name
            </Label>
            <Input
              id="eng-name"
              {...form.register("name")}
              className="mt-1 border-slate-600 bg-slate-800 text-white"
            />
            {form.formState.errors.name && (
              <p className="mt-1 text-sm text-red-400">{form.formState.errors.name.message}</p>
            )}
          </div>
          <div>
            <Label htmlFor="eng-desc" className="text-slate-300">
              Description (optional)
            </Label>
            <Textarea
              id="eng-desc"
              rows={3}
              {...form.register("description")}
              className="mt-1 border-slate-600 bg-slate-800 text-white"
            />
          </div>
          <div className="flex justify-end gap-2 pt-2">
            <Button type="button" variant="ghost" className="text-slate-400" onClick={() => onOpenChange(false)}>
              Cancel
            </Button>
            <Button
              type="submit"
              disabled={createEngagement.isPending}
              className="bg-emerald-600 hover:bg-emerald-500"
            >
              {createEngagement.isPending ? "Creating…" : "Create"}
            </Button>
          </div>
        </form>
      </DialogContent>
    </Dialog>
  );
}
