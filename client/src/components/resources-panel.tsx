import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";
import { useContainerMetrics } from "@/hooks/useContainerMetrics";

interface ResourcesPanelProps {
  className?: string;
  taskId: string;
}

export function ResourcesPanel({ className, taskId }: ResourcesPanelProps) {
  const { metrics, isConnected, error } = useContainerMetrics(taskId);

  const cpuUsage = metrics?.cpu_percent ?? 0;
  const ramUsage = metrics?.memory_percent ?? 0;
  const storageUsedMB = metrics?.storage?.used_mb ?? 0;
  return (
    <Card className={cn("bg-slate-800 border-slate-700 h-fit", className)}>
      <CardHeader className="pb-3">
        <CardTitle className="flex items-center justify-between text-white">
          <h4 className="text-white font-medium text-sm">Resources</h4>
          <Badge
            variant="secondary"
            className={`text-white text-xs px-2 py-1 ${
              isConnected ? 'bg-purple-600' : 'bg-gray-600'
            }`}
          >
            {isConnected ? 'Core' : 'Offline'}
          </Badge>
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-3">
        <p className="text-gray-400 text-xs">Resources for cloud development environment</p>
        
        {/* Compute Section */}
        <div className="space-y-2">
          <div className="flex items-center justify-between">
            <h5 className="text-white font-medium text-sm">Compute</h5>
            <span className="text-gray-400 text-xs">
              {metrics
                ? `${(metrics.memory_limit_mb / 1024).toFixed(1)} GiB RAM`
                : '4 vCPU, 8 GiB RAM'}
            </span>
          </div>
          
          {/* CPU */}
          <div className="space-y-1">
            <div className="flex items-center justify-between text-xs">
              <span className="text-gray-400">CPU</span>
              <div className="flex items-center space-x-2">
                <span className="text-white font-medium">{cpuUsage.toFixed(0)}%</span>
                <div className="w-6 h-3 bg-slate-700 rounded-sm overflow-hidden">
                  <div
                    className={`h-full transition-all duration-300 ${
                      cpuUsage > 80 ? 'bg-red-500' : cpuUsage > 60 ? 'bg-yellow-500' : 'bg-blue-500'
                    }`}
                    style={{ width: `${Math.min(cpuUsage, 100)}%` }}
                  />
                </div>
              </div>
            </div>
          </div>

          {/* RAM */}
          <div className="space-y-1">
            <div className="flex items-center justify-between text-xs">
              <span className="text-gray-400">RAM</span>
              <div className="flex items-center space-x-2">
                <span className="text-white font-medium">{ramUsage.toFixed(0)}%</span>
                <div className="w-6 h-3 bg-slate-700 rounded-sm overflow-hidden">
                  <div
                    className={`h-full transition-all duration-300 ${
                      ramUsage > 80 ? 'bg-red-500' : ramUsage > 60 ? 'bg-yellow-500' : 'bg-blue-500'
                    }`}
                    style={{ width: `${Math.min(ramUsage, 100)}%` }}
                  />
                </div>
              </div>
            </div>
          </div>
        </div>

        {/* Storage Section */}
        <div className="space-y-2 pt-2 border-t border-slate-700">
          <h5 className="text-white font-medium text-sm">Storage</h5>
          
          {/* This App Storage */}
          <div className="space-y-1">
            <div className="flex items-center justify-between text-xs">
              <span className="text-gray-400">This App</span>
              <span className="text-white font-medium">
                {storageUsedMB.toFixed(0)} MB
              </span>
            </div>
          </div>
        </div>
        {error && (
          <div className="pt-2 border-t border-slate-700">
            <div className="flex items-center text-xs text-red-400">
              <div className="w-2 h-2 bg-red-500 rounded-full mr-2" />
              Metrics unavailable
            </div>
          </div>
        )}
      </CardContent>
    </Card>
  );
}