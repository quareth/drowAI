/**
 * Create-task dialog: name, optional engagement (combobox), scope, VPN, and POST /api/tasks/.
 */

import { useEffect, useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { useForm } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import { z } from "zod";

import { EngagementCombobox } from "@/components/engagements/engagement-combobox";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { FileDropUpload } from "@/components/ui/file-drop-upload";
import { VPNConfigForm, type VPNConfig } from "@/components/vpn/VPNConfigForm";
import { apiRequest, queryClient } from "@/lib/queryClient";
import { responseToError } from "@/lib/response-error";
import { taskAdmissionErrorPresentation } from "@/lib/task-admission-errors";
import { invalidateEngagementKnowledgeQueries } from "@/hooks/use-engagement-knowledge";
import { useToast } from "@/hooks/use-toast";
import type { Task } from "@/types";

const insertTaskSchema = z.object({
  name: z.string().min(1, "Task name is required"),
  description: z.string().optional(),
  scope: z.string().optional(),
});

const formSchema = insertTaskSchema.extend({
  scopeFile: z.any().optional(),
  vpnEnabled: z.boolean().optional(),
  vpnConfig: z
    .object({
      provider: z.enum(["htb", "tryhackme", "custom"]).optional(),
      config_data: z.string().optional(),
    })
    .optional(),
});

type FormData = z.infer<typeof formSchema>;

function upsertTaskById(current: Task[] | undefined, task: Task): Task[] {
  const tasks = current ?? [];
  const existingIndex = tasks.findIndex((candidate) => candidate.id === task.id);
  if (existingIndex === -1) {
    return [task, ...tasks];
  }
  const next = [...tasks];
  next[existingIndex] = task;
  return next;
}

export interface NewTaskModalProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  preselectedEngagementId?: number | null;
  taskCountByEngagement?: Map<number, number>;
  canCreateTask?: boolean;
}

export function NewTaskModal({
  open,
  onOpenChange,
  preselectedEngagementId = null,
  taskCountByEngagement,
  canCreateTask = true,
}: NewTaskModalProps) {
  const [selectedEngagementId, setSelectedEngagementId] = useState<number | null>(null);
  const { toast } = useToast();

  const form = useForm<FormData>({
    resolver: zodResolver(formSchema),
    defaultValues: {
      name: "",
      scope: "",
      description: "",
    },
  });

  useEffect(() => {
    if (open) {
      setSelectedEngagementId(preselectedEngagementId ?? null);
    }
  }, [open, preselectedEngagementId]);

  useEffect(() => {
    if (!canCreateTask && open) {
      onOpenChange(false);
    }
  }, [canCreateTask, open, onOpenChange]);

  const createTaskMutation = useMutation({
    mutationFn: async (data: FormData) => {
      const taskData: Record<string, unknown> = {
        name: data.name,
        description: data.description || "",
        scope: data.scope,
      };
      if (selectedEngagementId !== null) {
        taskData.engagement_id = selectedEngagementId;
      }
      if (data.vpnEnabled && data.vpnConfig?.config_data) {
        taskData.vpn_enabled = true;
        taskData.vpn_config = {
          provider: data.vpnConfig.provider || "custom",
          config_data: data.vpnConfig.config_data,
        };
      }
      const response = await apiRequest("POST", "/api/tasks/", taskData);
      const taskResponse = response as Response;
      if (!taskResponse.ok) {
        throw await responseToError(taskResponse, "Failed to create task");
      }
      const newTask = (await taskResponse.json()) as Task;
      if (typeof newTask.id !== "number" || !Number.isFinite(newTask.id)) {
        throw new Error("Task creation response did not include a valid task id.");
      }
      return newTask;
    },
    onSuccess: async (newTask) => {
      queryClient.setQueryData<Task[]>(["/api/tasks/"], (current) => upsertTaskById(current, newTask));
      await queryClient.invalidateQueries({ queryKey: ["/api/tasks/"] });
      await invalidateEngagementKnowledgeQueries(queryClient, newTask?.engagement_id);
      if (newTask.status === "failed") {
        toast({
          title: "Task created but startup failed",
          description:
            newTask.error_message ||
            newTask.failure_reason ||
            "The task was saved, but runtime startup failed.",
          variant: "destructive",
        });
      } else {
        toast({
          title: "Task created successfully",
          description: "Your pentesting task has been created and is ready to start.",
        });
        try {
          await apiRequest("POST", `/api/tasks/${newTask.id}/chat/prewarm`);
        } catch (error) {
          console.warn("[chat-prewarm] Best-effort chat prewarm failed", error);
        }
      }
      window.dispatchEvent(new Event("taskCreated"));
      onOpenChange(false);
      form.reset();
      setSelectedEngagementId(null);
    },
    onError: (error: Error) => {
      if (error.message?.includes("already active")) {
        toast({
          title: "Duplicate task detected",
          description: "A task with this name is already running. Use a different name or wait for completion.",
          variant: "destructive",
        });
      } else {
        const presentation = taskAdmissionErrorPresentation(error, "Failed to create task");
        toast({
          title: presentation.title,
          description: presentation.description,
          variant: "destructive",
        });
      }
    },
    retry: false,
  });

  const onSubmit = (data: FormData) => {
    if (!canCreateTask) {
      return;
    }
    if (createTaskMutation.isPending) {
      return;
    }
    createTaskMutation.mutate(data);
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="flex max-h-[calc(100dvh-2rem)] w-[calc(100vw-2rem)] max-w-2xl flex-col overflow-hidden border-slate-700 bg-slate-900 p-0">
        <DialogHeader className="shrink-0 border-b border-slate-800 px-6 py-5 pr-12">
          <DialogTitle className="text-xl font-bold text-white">Create New Task</DialogTitle>
        </DialogHeader>

        <form onSubmit={form.handleSubmit(onSubmit)} className="flex min-h-0 flex-1 flex-col">
          <div data-testid="task-create-scroll-region" className="min-h-0 flex-1 space-y-6 overflow-y-auto px-6 py-5">
            <div>
              <Label htmlFor="name" className="mb-2 block text-sm font-medium text-gray-300">
                Task Name
              </Label>
              <Input
                id="name"
                placeholder="e.g., Web Application Assessment"
                {...form.register("name")}
                className="w-full border-slate-600 bg-slate-800 text-white focus:border-blue-500 focus:ring-1 focus:ring-blue-500"
              />
              {form.formState.errors.name && (
                <p className="mt-1 text-sm text-red-400">{form.formState.errors.name.message}</p>
              )}
            </div>

            <div>
              <Label className="mb-2 block text-sm font-medium text-gray-300">Engagement</Label>
              <EngagementCombobox
                value={selectedEngagementId}
                onChange={setSelectedEngagementId}
                disabled={!canCreateTask || createTaskMutation.isPending}
                taskCountByEngagement={taskCountByEngagement}
              />
            </div>

            <div>
              <Label htmlFor="scope" className="mb-2 block text-sm font-medium text-gray-300">
                Target Scope
              </Label>
              <Textarea
                id="scope"
                placeholder={`target.example.com\n192.168.1.0/24\napi.client.com`}
                rows={3}
                {...form.register("scope")}
                className="w-full border-slate-600 bg-slate-800 font-mono text-sm text-white focus:border-blue-500 focus:ring-1 focus:ring-blue-500"
              />
              {form.formState.errors.scope && (
                <p className="mt-1 text-sm text-red-400">{form.formState.errors.scope.message}</p>
              )}
            </div>

            {canCreateTask ? (
              <FileDropUpload
                label="Or Upload Scope File"
                accept={[".md", ".txt"]}
                inputId="scope-upload"
                emptyHint="Drag and drop a scope.md file or click to browse"
                onLoadText={(text, file) => {
                  form.setValue("scope", text);
                  toast({
                    title: "File uploaded successfully",
                    description: `${file.name} has been loaded into the scope field.`,
                  });
                }}
              />
            ) : null}

            <div className="space-y-3 rounded-md border border-slate-700 p-4">
              <div className="flex items-center justify-between">
                <Label className="text-gray-300">Enable VPN</Label>
                <Checkbox
                  checked={!!form.watch("vpnEnabled")}
                  onCheckedChange={(v) => form.setValue("vpnEnabled", !!v)}
                  disabled={!canCreateTask || createTaskMutation.isPending}
                />
              </div>
              {form.watch("vpnEnabled") && (
                <VPNConfigForm
                  onConfigChange={(cfg: VPNConfig) => form.setValue("vpnConfig", cfg as FormData["vpnConfig"])}
                  initialConfig={form.watch("vpnConfig") as VPNConfig | undefined}
                />
              )}
            </div>
          </div>

          <div className="flex shrink-0 justify-end space-x-3 border-t border-slate-800 bg-slate-900 px-6 py-4">
            <Button
              type="button"
              variant="ghost"
              onClick={() => onOpenChange(false)}
              className="text-gray-400 hover:text-white"
            >
              Cancel
            </Button>
            <Button
              type="submit"
              className="bg-blue-600 text-white shadow-glow-sm hover:bg-blue-700 hover:shadow-glow"
              disabled={!canCreateTask || createTaskMutation.isPending}
            >
              {createTaskMutation.isPending ? "Creating..." : "Create Task"}
            </Button>
          </div>
        </form>
      </DialogContent>
    </Dialog>
  );
}
