// Shared types for DrowAI frontend to match FastAPI backend models
export interface User {
  id: number;
  username: string;
  email?: string;
  created_at: string;
  is_active: boolean;
}

export interface Task {
  id: number;
  user_id: number;
  engagement_id?: number | null;
  engagement_name?: string | null;
  name: string;
  description?: string;
  scope?: string;
  status: string;
  created_at: string;
  updated_at: string;
  error_message?: string | null;
  failure_reason?: string | null;
  sshPort?: number;
  ssh_port?: number;
}

export interface Report {
  id: number;
  task_id: number;
  user_id: number;
  title: string;
  content: string;
  findings?: Record<string, any>;
  severity?: string;
  created_at: string;
}

// Container Management Types
export interface ContainerStatus {
  task_id: number;
  container_exists: boolean;
  status: string;
  details: ContainerDetails;
}

export interface ContainerDetails {
  id?: string;
  name?: string;
  status?: string;
  created?: string;
  started_at?: string;
  finished_at?: string;
  exit_code?: number;
  image?: string;
  ports?: Record<string, any>;
  mounts?: string[];
  resource_usage?: ContainerResourceUsage;
}

export interface ContainerResourceUsage {
  cpu_percent: number;
  memory_usage_mb: number;
  memory_limit_mb: number;
  memory_percent: number;
}

export interface StorageStats {
  used_bytes: number;
  size_root_fs: number;
  used_mb: number;
  used_gb: number;
}

export interface NetworkStats {
  rx_bytes: number;
  tx_bytes: number;
  rx_rate?: number;
  tx_rate?: number;
}

export interface ContainerMetrics {
  cpu_percent: number;
  memory_usage_mb: number;
  memory_limit_mb: number;
  memory_percent: number;
  storage: StorageStats;
  network: NetworkStats;
  timestamp: string;
}

export interface ContainerListResponse {
  containers: ContainerInfo[];
  total: number;
}

export interface ContainerInfo {
  task_id: number;
  container_id: string;
  name: string;
  status: string;
  image: string;
  created: string;
}

export interface CommandExecutionResult {
  success: boolean;
  stdout: string;
  stderr: string;
  command: string;
}

// Re-export usage types
export type {
  TokenUsage,
  UsageBreakdownItem,
  UsageBreakdownResponse,
  LegacyUsageCostResponse,
} from "./usage";

export {
  formatCostUSD,
  formatTokenCount,
  getUsageSummary,
} from "./usage";
