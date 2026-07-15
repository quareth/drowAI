import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { 
  Target, 
  Shield, 
  Clock, 
  FileText, 
  AlertTriangle, 
  CheckCircle,
  Globe,
  Network,
  Link,
  AlertCircle,
  ListTodo,
  Settings,
  Download
} from "lucide-react";
import { cn } from "@/lib/utils";

interface ScopeDetailsModalProps {
  isOpen: boolean;
  onClose: () => void;
  taskId: number;
  taskName: string;
}

interface ParsedTarget {
  type: string;
  normalized: string;
  raw: string;
}

interface ParsedConstraint {
  type: string;
  raw: string;
  details: Record<string, any>;
}

interface ParsedScope {
  targets: ParsedTarget[];
  objectives: string[];
  constraints: ParsedConstraint[];
  methodology: string[];
  time_limit: number;
  testing_depth: string;
  output_format: string;
}

interface ScopeData {
  success: boolean;
  task_id: number;
  task_name: string;
  parsed_scope?: ParsedScope;
  validation_errors?: string[];
  warnings?: string[];
  has_errors?: boolean;
  error?: string;
  raw_scope?: string;
}

export function ScopeDetailsModal({ isOpen, onClose, taskId, taskName }: ScopeDetailsModalProps) {
  const [activeTab, setActiveTab] = useState("overview");

  const { data: scopeData, isLoading, error } = useQuery<ScopeData>({
    queryKey: [`/api/tasks/${taskId}/scope`],
    enabled: isOpen && !!taskId,
    refetchOnWindowFocus: false,
  });

  const getTargetIcon = (type: string) => {
    switch (type) {
      case 'ip':
      case 'ip_range':
        return <Network className="w-4 h-4" />;
      case 'domain':
        return <Globe className="w-4 h-4" />;
      case 'url':
        return <Link className="w-4 h-4" />;
      case 'cidr':
        return <Network className="w-4 h-4" />;
      default:
        return <Target className="w-4 h-4" />;
    }
  };

  const getTargetTypeColor = (type: string) => {
    switch (type) {
      case 'ip':
        return 'bg-blue-500/20 text-blue-400 border-blue-500/30';
      case 'domain':
        return 'bg-green-500/20 text-green-400 border-green-500/30';
      case 'url':
        return 'bg-purple-500/20 text-purple-400 border-purple-500/30';
      case 'cidr':
        return 'bg-orange-500/20 text-orange-400 border-orange-500/30';
      case 'ip_range':
        return 'bg-cyan-500/20 text-cyan-400 border-cyan-500/30';
      default:
        return 'bg-gray-500/20 text-gray-400 border-gray-500/30';
    }
  };

  const getConstraintTypeColor = (type: string) => {
    switch (type) {
      case 'timing':
        return 'bg-yellow-500/20 text-yellow-400 border-yellow-500/30';
      case 'rate_limit':
        return 'bg-red-500/20 text-red-400 border-red-500/30';
      case 'exclusion':
        return 'bg-orange-500/20 text-orange-400 border-orange-500/30';
      default:
        return 'bg-blue-500/20 text-blue-400 border-blue-500/30';
    }
  };

  const getTestingDepthColor = (depth: string) => {
    switch (depth) {
      case 'comprehensive':
        return 'bg-red-500/20 text-red-400 border-red-500/30';
      case 'deep':
        return 'bg-orange-500/20 text-orange-400 border-orange-500/30';
      case 'surface':
        return 'bg-green-500/20 text-green-400 border-green-500/30';
      default:
        return 'bg-blue-500/20 text-blue-400 border-blue-500/30';
    }
  };

  const formatTimeLimit = (minutes: number) => {
    if (minutes >= 60) {
      const hours = Math.floor(minutes / 60);
      const remainingMinutes = minutes % 60;
      return remainingMinutes > 0 
        ? `${hours}h ${remainingMinutes}m`
        : `${hours}h`;
    }
    return `${minutes}m`;
  };

  const renderLoadingState = () => (
    <div className="flex items-center justify-center py-12">
      <div className="text-center">
        <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-blue-400 mx-auto mb-4"></div>
        <p className="text-gray-400">Loading scope details...</p>
      </div>
    </div>
  );

  const renderErrorState = () => (
    <div className="py-8">
      <Alert className="border-red-500/50 bg-red-500/10">
        <AlertTriangle className="w-4 h-4 text-red-400" />
        <AlertDescription className="text-red-400">
          {error?.message || "Failed to load scope details"}
        </AlertDescription>
      </Alert>
    </div>
  );

  const renderValidationAlerts = () => {
    if (!scopeData?.success || !scopeData.validation_errors?.length && !scopeData.warnings?.length) {
      return null;
    }

    return (
      <div className="space-y-3 mb-6">
        {scopeData.validation_errors?.map((error, index) => (
          <Alert key={`error-${index}`} className="border-red-500/50 bg-red-500/10">
            <AlertTriangle className="w-4 h-4 text-red-400" />
            <AlertDescription className="text-red-400">{error}</AlertDescription>
          </Alert>
        ))}
        {scopeData.warnings?.map((warning, index) => (
          <Alert key={`warning-${index}`} className="border-yellow-500/50 bg-yellow-500/10">
            <AlertCircle className="w-4 h-4 text-yellow-400" />
            <AlertDescription className="text-yellow-400">{warning}</AlertDescription>
          </Alert>
        ))}
      </div>
    );
  };

  const renderOverviewTab = () => {
    if (!scopeData?.success || !scopeData.parsed_scope) {
      return (
        <div className="space-y-4">
          <Alert className="border-orange-500/50 bg-orange-500/10">
            <AlertTriangle className="w-4 h-4 text-orange-400" />
            <AlertDescription className="text-orange-400">
              {scopeData?.error || "Scope parsing failed"}
            </AlertDescription>
          </Alert>
          {scopeData?.raw_scope && (
            <Card className="bg-slate-800 border-slate-700">
              <CardHeader>
                <CardTitle className="text-white flex items-center">
                  <FileText className="w-4 h-4 mr-2" />
                  Raw Scope Content
                </CardTitle>
              </CardHeader>
              <CardContent>
                <pre className="text-sm text-gray-300 whitespace-pre-wrap bg-slate-900 p-4 rounded">
                  {scopeData.raw_scope}
                </pre>
              </CardContent>
            </Card>
          )}
        </div>
      );
    }

    const { parsed_scope } = scopeData;

    return (
      <div className="space-y-6">
        {/* Quick Stats */}
        <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
          <div className="bg-slate-800 border border-slate-700 rounded-lg p-4">
            <div className="flex items-center space-x-2 mb-1">
              <Target className="w-4 h-4 text-blue-400" />
              <span className="text-sm text-gray-400">Targets</span>
            </div>
            <p className="text-xl font-semibold text-white">{parsed_scope.targets.length}</p>
          </div>
          <div className="bg-slate-800 border border-slate-700 rounded-lg p-4">
            <div className="flex items-center space-x-2 mb-1">
              <ListTodo className="w-4 h-4 text-green-400" />
              <span className="text-sm text-gray-400">Objectives</span>
            </div>
            <p className="text-xl font-semibold text-white">{parsed_scope.objectives.length}</p>
          </div>
          <div className="bg-slate-800 border border-slate-700 rounded-lg p-4">
            <div className="flex items-center space-x-2 mb-1">
              <Shield className="w-4 h-4 text-orange-400" />
              <span className="text-sm text-gray-400">Constraints</span>
            </div>
            <p className="text-xl font-semibold text-white">{parsed_scope.constraints.length}</p>
          </div>
          <div className="bg-slate-800 border border-slate-700 rounded-lg p-4">
            <div className="flex items-center space-x-2 mb-1">
              <Clock className="w-4 h-4 text-purple-400" />
              <span className="text-sm text-gray-400">Time Limit</span>
            </div>
            <p className="text-xl font-semibold text-white">{formatTimeLimit(parsed_scope.time_limit)}</p>
          </div>
        </div>

        {/* Testing Configuration */}
        <Card className="bg-slate-800 border-slate-700">
          <CardHeader>
            <CardTitle className="text-white flex items-center">
              <Settings className="w-4 h-4 mr-2" />
              Testing Configuration
            </CardTitle>
          </CardHeader>
          <CardContent className="space-y-4">
            <div className="flex items-center justify-between">
              <span className="text-gray-400">Testing Depth</span>
              <Badge className={cn("border", getTestingDepthColor(parsed_scope.testing_depth))}>
                {parsed_scope.testing_depth.charAt(0).toUpperCase() + parsed_scope.testing_depth.slice(1)}
              </Badge>
            </div>
            <div className="flex items-center justify-between">
              <span className="text-gray-400">Output Format</span>
              <Badge className="bg-blue-500/20 text-blue-400 border-blue-500/30">
                {parsed_scope.output_format.toUpperCase()}
              </Badge>
            </div>
            <div className="flex items-center justify-between">
              <span className="text-gray-400">Time Allocation</span>
              <span className="text-white font-medium">{formatTimeLimit(parsed_scope.time_limit)}</span>
            </div>
          </CardContent>
        </Card>
      </div>
    );
  };

  const renderTargetsTab = () => {
    if (!scopeData?.success || !scopeData.parsed_scope?.targets.length) {
      return (
        <div className="text-center py-8">
          <Target className="w-12 h-12 mx-auto mb-4 text-gray-600" />
          <p className="text-gray-400">No targets defined</p>
        </div>
      );
    }

    const { targets } = scopeData.parsed_scope;
    const targetsByType = targets.reduce((acc, target) => {
      if (!acc[target.type]) acc[target.type] = [];
      acc[target.type].push(target);
      return acc;
    }, {} as Record<string, ParsedTarget[]>);

    return (
      <div className="space-y-6">
        {Object.entries(targetsByType).map(([type, typeTargets]) => (
          <Card key={type} className="bg-slate-800 border-slate-700">
            <CardHeader>
              <CardTitle className="text-white flex items-center">
                {getTargetIcon(type)}
                <span className="ml-2 capitalize">{type.replace('_', ' ')} Targets</span>
                <Badge className={cn("ml-auto", getTargetTypeColor(type))}>
                  {typeTargets.length}
                </Badge>
              </CardTitle>
            </CardHeader>
            <CardContent className="max-h-96 overflow-y-auto">
              <div className="space-y-2">
                {typeTargets.map((target, index) => (
                  <div key={index} className="flex items-center justify-between p-2 bg-slate-900 rounded-lg border border-slate-700/50 hover:border-slate-600 transition-colors">
                    <div className="flex-1 min-w-0">
                      <p className="font-medium text-white truncate">{target.normalized}</p>
                      {target.raw !== target.normalized && (
                        <p className="text-xs text-gray-400 truncate">Raw: {target.raw}</p>
                      )}
                    </div>
                    <Badge className={cn("ml-2 flex-shrink-0", getTargetTypeColor(target.type))}>
                      {target.type}
                    </Badge>
                  </div>
                ))}
              </div>
            </CardContent>
          </Card>
        ))}
      </div>
    );
  };

  const renderObjectivesTab = () => {
    if (!scopeData?.success || !scopeData.parsed_scope?.objectives.length) {
      return (
        <div className="text-center py-8">
          <ListTodo className="w-12 h-12 mx-auto mb-4 text-gray-600" />
          <p className="text-gray-400">No objectives defined</p>
        </div>
      );
    }

    return (
      <Card className="bg-slate-800 border-slate-700 h-full">
        <CardHeader>
          <CardTitle className="text-white flex items-center">
            <ListTodo className="w-4 h-4 mr-2" />
            Testing Objectives
            <Badge className="ml-auto bg-green-500/20 text-green-400">
              {scopeData.parsed_scope.objectives.length} items
            </Badge>
          </CardTitle>
        </CardHeader>
        <CardContent className="max-h-96 overflow-y-auto">
          <div className="space-y-3">
            {scopeData.parsed_scope.objectives.map((objective, index) => (
              <div key={index} className="flex items-start space-x-3 p-3 bg-slate-900 rounded-lg border border-slate-700/50 hover:border-slate-600 transition-colors">
                <div className="flex-shrink-0 w-6 h-6 bg-green-500/20 rounded-full flex items-center justify-center mt-1">
                  <span className="text-xs font-medium text-green-400">{index + 1}</span>
                </div>
                <p className="text-white leading-relaxed">{objective}</p>
              </div>
            ))}
          </div>
        </CardContent>
      </Card>
    );
  };

  const renderConstraintsTab = () => {
    if (!scopeData?.success || !scopeData.parsed_scope?.constraints.length) {
      return (
        <div className="text-center py-8">
          <Shield className="w-12 h-12 mx-auto mb-4 text-gray-600" />
          <p className="text-gray-400">No constraints defined</p>
        </div>
      );
    }

    const { constraints } = scopeData.parsed_scope;
    const constraintsByType = constraints.reduce((acc, constraint) => {
      if (!acc[constraint.type]) acc[constraint.type] = [];
      acc[constraint.type].push(constraint);
      return acc;
    }, {} as Record<string, ParsedConstraint[]>);

    return (
      <div className="space-y-6">
        {Object.entries(constraintsByType).map(([type, typeConstraints]) => (
          <Card key={type} className="bg-slate-800 border-slate-700">
            <CardHeader>
              <CardTitle className="text-white flex items-center">
                <Shield className="w-4 h-4 mr-2" />
                <span className="capitalize">{type.replace('_', ' ')} Constraints</span>
                <Badge className={cn("ml-auto", getConstraintTypeColor(type))}>
                  {typeConstraints.length}
                </Badge>
              </CardTitle>
            </CardHeader>
            <CardContent className="max-h-96 overflow-y-auto">
              <div className="space-y-2">
                {typeConstraints.map((constraint, index) => (
                  <div key={index} className="p-3 bg-slate-900 rounded-lg border border-slate-700/50 hover:border-slate-600 transition-colors">
                    <p className="text-white mb-2 leading-relaxed">{constraint.raw}</p>
                    {Object.keys(constraint.details).length > 0 && (
                      <div className="text-xs text-gray-400 bg-slate-800 p-2 rounded mt-2">
                        <span className="font-medium">Details: </span>
                        <span className="break-all">{JSON.stringify(constraint.details)}</span>
                      </div>
                    )}
                  </div>
                ))}
              </div>
            </CardContent>
          </Card>
        ))}
      </div>
    );
  };

  const renderMethodologyTab = () => {
    if (!scopeData?.success || !scopeData.parsed_scope?.methodology.length) {
      return (
        <div className="text-center py-8">
          <Settings className="w-12 h-12 mx-auto mb-4 text-gray-600" />
          <p className="text-gray-400">No methodology defined</p>
        </div>
      );
    }

    return (
      <div className="space-y-4">
        <Card className="bg-slate-800 border-slate-700">
          <CardHeader>
            <CardTitle className="text-white flex items-center">
              <Settings className="w-4 h-4 mr-2" />
              Testing Methodology
              <Badge className="ml-auto bg-purple-500/20 text-purple-400">
                {scopeData.parsed_scope.methodology.length} items
              </Badge>
            </CardTitle>
          </CardHeader>
          <CardContent className="max-h-96 overflow-y-auto">
            <div className="space-y-2">
              {scopeData.parsed_scope.methodology.map((method, index) => (
                <div key={index} className="flex items-start space-x-3 p-2 bg-slate-900 rounded-lg border border-slate-700/50 hover:border-slate-600 transition-colors">
                  <div className="flex-shrink-0 w-6 h-6 bg-purple-500/20 rounded-full flex items-center justify-center mt-1">
                    <span className="text-xs font-medium text-purple-400">{index + 1}</span>
                  </div>
                  <p className="text-white leading-relaxed">{method}</p>
                </div>
              ))}
            </div>
          </CardContent>
        </Card>
        
        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
          <Card className="bg-slate-800 border-slate-700">
            <CardContent className="p-4">
              <div className="flex items-center space-x-2 mb-2">
                <Clock className="w-4 h-4 text-orange-400" />
                <span className="text-sm font-medium text-slate-300">Time Allocation</span>
              </div>
              <div className="text-lg font-bold text-white">
                {scopeData.parsed_scope.time_limit ? `${Math.floor(scopeData.parsed_scope.time_limit / 60)}h ${scopeData.parsed_scope.time_limit % 60}m` : '4h 0m'}
              </div>
            </CardContent>
          </Card>
          
          <Card className="bg-slate-800 border-slate-700">
            <CardContent className="p-4">
              <div className="flex items-center space-x-2 mb-2">
                <FileText className="w-4 h-4 text-blue-400" />
                <span className="text-sm font-medium text-slate-300">Output Format</span>
              </div>
              <div className="text-lg font-bold text-white capitalize">
                {scopeData.parsed_scope.output_format || 'markdown'}
              </div>
            </CardContent>
          </Card>
        </div>
      </div>
    );
  };

  return (
    <Dialog open={isOpen} onOpenChange={onClose}>
      <DialogContent className="max-w-5xl w-[90vw] h-[85vh] bg-slate-900 border-slate-700 flex flex-col">
        <DialogHeader className="flex-shrink-0">
          <DialogTitle className="text-xl font-bold text-white flex items-center">
            <FileText className="w-5 h-5 mr-2 text-blue-400" />
            Scope Details - {taskName}
          </DialogTitle>
        </DialogHeader>

        <div className="flex flex-col flex-1 min-h-0">
          {/* Validation Alerts */}
          <div className="flex-shrink-0">
            {renderValidationAlerts()}
          </div>

          {/* Status Indicator */}
          {scopeData && (
            <div className="flex items-center space-x-2 mb-4 flex-shrink-0">
              {scopeData.success ? (
                <CheckCircle className="w-4 h-4 text-green-400" />
              ) : (
                <AlertTriangle className="w-4 h-4 text-orange-400" />
              )}
              <span className="text-sm text-gray-400">
                {scopeData.success ? "Scope parsed successfully" : "Scope parsing failed"}
              </span>
            </div>
          )}

          {/* Content */}
          <div className="flex-1 min-h-0 overflow-hidden">
            {isLoading ? (
              renderLoadingState()
            ) : error ? (
              renderErrorState()
            ) : (
              <Tabs value={activeTab} onValueChange={setActiveTab} className="h-full flex flex-col">
                <TabsList className="grid w-full grid-cols-5 bg-slate-800 border-slate-700 flex-shrink-0">
                  <TabsTrigger value="overview" className="data-[state=active]:bg-slate-700">
                    Overview
                  </TabsTrigger>
                  <TabsTrigger value="targets" className="data-[state=active]:bg-slate-700">
                    Targets
                  </TabsTrigger>
                  <TabsTrigger value="objectives" className="data-[state=active]:bg-slate-700">
                    Objectives
                  </TabsTrigger>
                  <TabsTrigger value="constraints" className="data-[state=active]:bg-slate-700">
                    Constraints
                  </TabsTrigger>
                  <TabsTrigger value="methodology" className="data-[state=active]:bg-slate-700">
                    Methodology
                  </TabsTrigger>
                </TabsList>

                <div className="flex-1 mt-4 min-h-0 overflow-hidden">
                  <ScrollArea className="h-full scrollbar-show-on-hover">
                    <div className="p-1">
                      <TabsContent value="overview" className="mt-0 data-[state=active]:block hidden">
                        {renderOverviewTab()}
                      </TabsContent>
                      <TabsContent value="targets" className="mt-0 data-[state=active]:block hidden">
                        {renderTargetsTab()}
                      </TabsContent>
                      <TabsContent value="objectives" className="mt-0 data-[state=active]:block hidden">
                        {renderObjectivesTab()}
                      </TabsContent>
                      <TabsContent value="constraints" className="mt-0 data-[state=active]:block hidden">
                        {renderConstraintsTab()}
                      </TabsContent>
                      <TabsContent value="methodology" className="mt-0 data-[state=active]:block hidden">
                        {renderMethodologyTab()}
                      </TabsContent>
                    </div>
                  </ScrollArea>
                </div>
              </Tabs>
            )}
          </div>

          {/* Footer */}
          <div className="flex items-center justify-between pt-4 border-t border-slate-700">
            <div className="text-sm text-gray-400">
              Task ID: {taskId}
            </div>
            <div className="flex space-x-2">
              <Button variant="outline" size="sm" className="border-slate-600 text-gray-400">
                <Download className="w-4 h-4 mr-2" />
                Export
              </Button>
              <Button onClick={onClose} className="bg-blue-600 hover:bg-blue-700">
                Close
              </Button>
            </div>
          </div>
        </div>
      </DialogContent>
    </Dialog>
  );
}
