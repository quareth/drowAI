/**
 * Generated report library workspace.
 *
 * Responsibility: own all generated-report artifact browsing, preview,
 * Markdown download, and delete/undo state independently of engagement report
 * generation.
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import { RefreshCw } from "lucide-react";
import { useQueryClient } from "@tanstack/react-query";

import { ReportLibraryList } from "@/components/reporting/report-library-list";
import { ReportPreview } from "@/components/reporting/report-preview";
import { Button } from "@/components/ui/button";
import { ToastAction } from "@/components/ui/toast";
import {
  reportingKeys,
  useDeleteEngagementReport,
  useEngagementReport,
  useReportLibrary,
  useUndoDeleteEngagementReport,
} from "@/hooks/use-reporting";
import { useToast } from "@/hooks/use-toast";
import { cn } from "@/lib/utils";
import type { EngagementReportReadResponse, ReportType } from "@/types/reporting";

const LIBRARY_REPORT_TYPE: ReportType = "pentest";
const LIBRARY_PAGE_SIZE = 50;

export function ReportLibraryWorkspace() {
  const queryClient = useQueryClient();
  const { toast } = useToast();
  const [libraryOffset, setLibraryOffset] = useState(0);
  const libraryFilters = useMemo(
    () => ({
      report_type: LIBRARY_REPORT_TYPE,
      limit: LIBRARY_PAGE_SIZE,
      offset: libraryOffset,
    }),
    [libraryOffset],
  );
  const [selectedReportId, setSelectedReportId] = useState<string | null>(null);

  const libraryQuery = useReportLibrary(libraryFilters);
  const selectedReportQuery = useEngagementReport(selectedReportId);
  const deleteReportMutation = useDeleteEngagementReport();
  const undoDeleteReportMutation = useUndoDeleteEngagementReport();

  const reports = libraryQuery.data?.reports ?? [];
  const previewReport = selectedReportQuery.data ?? null;
  const isRefreshing = libraryQuery.isFetching || selectedReportQuery.isFetching;

  useEffect(() => {
    if (
      selectedReportId !== null &&
      libraryQuery.isSuccess &&
      !reports.some((report) => report.report_id === selectedReportId)
    ) {
      setSelectedReportId(null);
    }
  }, [libraryQuery.isSuccess, reports, selectedReportId]);

  const handleRefresh = useCallback(() => {
    void queryClient.invalidateQueries({
      queryKey: reportingKeys.library(libraryFilters),
    });
  }, [libraryFilters, queryClient]);

  const handleOpenReport = useCallback((reportId: string) => {
    setSelectedReportId(reportId);
  }, []);

  const handlePreviousPage = useCallback(() => {
    setLibraryOffset((offset) => Math.max(0, offset - LIBRARY_PAGE_SIZE));
  }, []);

  const handleNextPage = useCallback(() => {
    setLibraryOffset((offset) => offset + LIBRARY_PAGE_SIZE);
  }, []);

  const handleUndoDeleteReport = useCallback(
    async (reportId: string) => {
      try {
        await undoDeleteReportMutation.mutateAsync({
          report_id: reportId,
          report_type: LIBRARY_REPORT_TYPE,
        });
        toast({
          title: "Report restored",
          description: "The report deletion was undone.",
        });
      } catch (error) {
        toast({
          title: "Undo failed",
          description:
            error instanceof Error ? error.message : "Could not restore the report.",
          variant: "destructive",
        });
      }
    },
    [toast, undoDeleteReportMutation],
  );

  const handleDeleteReport = useCallback(
    async (report: EngagementReportReadResponse) => {
      try {
        await deleteReportMutation.mutateAsync({
          report_id: report.id,
          report_type: report.report_type,
        });
        if (selectedReportId === report.id) {
          setSelectedReportId(null);
        }
        toast({
          title: "Report deleted",
          description: "The generated report was removed from the library.",
          action: (
            <ToastAction
              altText="Undo report deletion"
              onClick={() => void handleUndoDeleteReport(report.id)}
            >
              Undo
            </ToastAction>
          ),
        });
      } catch (error) {
        toast({
          title: "Delete failed",
          description:
            error instanceof Error ? error.message : "Could not delete the report.",
          variant: "destructive",
        });
      }
    },
    [deleteReportMutation, handleUndoDeleteReport, selectedReportId, toast],
  );

  return (
    <main className="flex-1 overflow-auto bg-slate-950 p-4 md:p-6">
      <div className="mx-auto grid min-h-full max-w-[90rem] gap-4 xl:grid-cols-[minmax(28rem,0.46fr)_minmax(48rem,1fr)]">
        <section className="min-w-0 space-y-4">
          <div className="flex items-center justify-between gap-3">
            <div>
              <h1 className="text-2xl font-semibold text-white">Generated Reports</h1>
              <p className="mt-1 text-sm text-slate-400">
                Generated reports remain available as tenant-owned artifacts.
              </p>
            </div>
            <Button
              type="button"
              variant="outline"
              size="icon"
              className="h-9 w-9 border-slate-800 bg-slate-950 text-slate-300 hover:border-slate-700 hover:bg-slate-900 hover:text-white"
              onClick={handleRefresh}
              disabled={isRefreshing}
              aria-label="Refresh report library"
              title="Refresh report library"
            >
              <RefreshCw className={cn("h-4 w-4", isRefreshing && "animate-spin")} />
            </Button>
          </div>

          <ReportLibraryList
            reports={reports}
            total={libraryQuery.data?.total ?? reports.length}
            limit={libraryQuery.data?.limit ?? LIBRARY_PAGE_SIZE}
            offset={libraryQuery.data?.offset ?? libraryOffset}
            selectedReportId={selectedReportId}
            isLoading={libraryQuery.isLoading}
            isFetching={libraryQuery.isFetching}
            isError={libraryQuery.isError}
            errorMessage={libraryQuery.error?.message ?? null}
            onOpenReport={handleOpenReport}
            onPreviousPage={handlePreviousPage}
            onNextPage={handleNextPage}
          />
        </section>

        <aside className="min-w-0 xl:sticky xl:top-0 xl:self-start">
          <ReportPreview
            report={previewReport}
            isLoading={selectedReportId !== null && selectedReportQuery.isLoading}
            isError={selectedReportId !== null && selectedReportQuery.isError}
            errorMessage={selectedReportQuery.error?.message ?? null}
            emptyMessage="Select a generated report to preview and download."
            isDeleting={
              previewReport !== null &&
              deleteReportMutation.isPending &&
              deleteReportMutation.variables?.report_id === previewReport.id
            }
            onDeleteReport={handleDeleteReport}
          />
        </aside>
      </div>
    </main>
  );
}
