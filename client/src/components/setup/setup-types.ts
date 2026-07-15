/**
 * Shared frontend types and step metadata for the standalone setup wizard.
 */
export interface SetupStatus {
  setup_required: boolean;
  wizard_enabled: boolean;
  installation_complete: boolean;
  installation_status: "pending" | "provisioning" | "complete" | "failed";
  setup_error: string | null;
  deployment_profile: string;
  database_accessible: boolean;
  runner_connected: boolean;
}

export interface SetupDatabaseConfig {
  db_name: string;
  db_user: string;
  db_password: string;
  db_host?: string;
  db_port?: number;
}

export interface SetupSecurityConfig {
  session_timeout: number;
  admin_username: string;
  admin_email: string;
  admin_password: string;
}

export interface SetupDisplayConfig {
  timezone: string;
}

export interface SetupNetworkConfig {
  management_ip?: string;
  gateway?: string;
  dns_servers?: string;
  domain?: string;
  kali_docker_network?: string;
}

export interface SetupRunnerConfig {
  create_site: boolean;
  site_name: string;
  site_slug: string;
}

export interface SetupConfig {
  database: SetupDatabaseConfig;
  security: SetupSecurityConfig;
  display: SetupDisplayConfig;
  network: SetupNetworkConfig;
  runner: SetupRunnerConfig;
}

export interface SetupCompleteResponse {
  status: string;
  message: string;
  redirect?: string;
  admin_username: string;
  runner_site_created: boolean;
  runner_enrollment_published: boolean;
  runner_readiness: "ready" | "waiting_for_runner";
  runtime_services_started?: boolean;
  restart_required?: boolean;
}

export const SETUP_STEPS = [
  { id: 1, title: "Welcome", description: "Standalone installation" },
  { id: 2, title: "Database", description: "PostgreSQL connection" },
  { id: 3, title: "Security", description: "Admin account" },
  { id: 4, title: "Display", description: "Timezone preference" },
  { id: 5, title: "Runner", description: "Runner Site readiness" },
  { id: 6, title: "Complete", description: "Review and finish" },
] as const;
