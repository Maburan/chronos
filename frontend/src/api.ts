/**
 * Typed client for the Chronos HTTP API.
 *
 * This file intentionally does nothing beyond: build a request, attach the
 * bearer token if one is stored, call fetch, parse JSON, throw on a
 * non-2xx status. It does not retry, cache, or infer anything about task
 * state that the backend didn't already compute — every field here
 * mirrors api/schemas.py exactly, and every function here maps 1:1 onto
 * one route in api/routes.py or api/auth_routes.py.
 */

const API_BASE_URL = import.meta.env.VITE_CHRONOS_API_URL ?? "http://localhost:8000";
const TOKEN_STORAGE_KEY = "chronos.authToken";

export class ApiError extends Error {
  status: number;
  detail: unknown;

  constructor(status: number, detail: unknown) {
    super(`Chronos API error ${status}: ${JSON.stringify(detail)}`);
    this.status = status;
    this.detail = detail;
  }
}

// ---------------------------------------------------------------------------
// Token storage — a plain localStorage string, read fresh on every request
// so every tab/component sees the same value without needing its own
// subscription mechanism. auth/AuthContext.tsx is the only thing that
// calls setAuthToken/clearAuthToken; every other caller just uses the API
// functions below and gets the header attached automatically.
// ---------------------------------------------------------------------------

export function getAuthToken(): string | null {
  return localStorage.getItem(TOKEN_STORAGE_KEY);
}

export function setAuthToken(token: string): void {
  localStorage.setItem(TOKEN_STORAGE_KEY, token);
}

export function clearAuthToken(): void {
  localStorage.removeItem(TOKEN_STORAGE_KEY);
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const token = getAuthToken();
  const res = await fetch(`${API_BASE_URL}${path}`, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      ...(init?.headers ?? {}),
    },
  });
  if (!res.ok) {
    let detail: unknown;
    try {
      detail = await res.json();
    } catch {
      detail = await res.text();
    }
    throw new ApiError(res.status, detail);
  }
  // 204/empty-body responses aren't used by this API today, but guard anyway.
  const text = await res.text();
  return (text ? JSON.parse(text) : undefined) as T;
}

// ---------------------------------------------------------------------------
// Shapes — mirror api/schemas.py field-for-field.
// ---------------------------------------------------------------------------

export interface User {
  id: number;
  email: string;
  created_at: string;
}

export interface AuthResponse {
  access_token: string;
  token_type: string;
  user: User;
}

export interface Workflow {
  id: number;
  name: string;
  user_id: number;
  created_at: string;
}

export type TaskState =
  | "PENDING"
  | "READY"
  | "RUNNING"
  | "SUCCEEDED"
  | "FAILED_TERMINAL"
  | "SKIPPED";

export interface Task {
  id: number;
  workflow_id: number;
  name: string;
  task_type: string;
  state: TaskState;
  attempt_count: number;
  max_attempts: number;
  lease_owner: string | null;
  lease_expires_at: string | null;
  created_at: string;
  updated_at: string;
  payload: Record<string, unknown> | null;
  depends_on_ids: number[];
  workflow_run_id?: number | null;
  task_definition_id?: number | null;
}

export interface WorkflowStatus {
  workflow_id: number;
  total_tasks: number;
  pending: number;
  ready: number;
  running: number;
  succeeded: number;
  failed_terminal: number;
  skipped: number;
  is_complete: boolean;
  has_failures: boolean;
}

export interface CreateTaskInput {
  name: string;
  task_type: string;
  max_attempts?: number;
  payload?: Record<string, unknown> | null;
  depends_on_ids?: number[] | null;
}

export interface TaskTypeField {
  name: string;
  type: string;
  required: boolean;
  default?: unknown;
  label?: string;
  help_text?: string;
  placeholder?: string;
  input_type?: string;
  advanced?: boolean;
  options?: string[];
  validation?: { min?: number; max?: number; pattern?: string };
}

export interface TaskTypeInfo {
  type: string;
  label: string;
  description: string;
  category?: string;
  icon?: string;
  fields: TaskTypeField[];
}

// ---------------------------------------------------------------------------
// Workflow definitions (Builder design-time graph -- Save/load) and runs
// (materialized executions). Mirrors api/schemas.py's
// TaskDefinitionInput/TaskDefinitionDependencyInput/
// SaveWorkflowDefinitionRequest/TaskDefinitionResponse/
// WorkflowDefinitionResponse/WorkflowRunResponse field-for-field. See
// PUT/GET /workflows/{id}/definition and POST /workflows/{id}/runs.
// ---------------------------------------------------------------------------

export interface TaskDefinitionInput {
  client_id: string;
  name: string;
  task_type: string;
  max_attempts?: number;
  payload?: Record<string, unknown> | null;
  position_x?: number | null;
  position_y?: number | null;
}

export interface TaskDefinitionDependencyInput {
  task_client_id: string;
  depends_on_client_id: string;
  branch?: string | null;
}

export interface TaskDefinition {
  id: number;
  workflow_id: number;
  name: string;
  task_type: string;
  max_attempts: number;
  payload: Record<string, unknown> | null;
  position_x: number | null;
  position_y: number | null;
  depends_on_ids: number[];
  created_at: string;
  updated_at: string;
}

export interface WorkflowDefinition {
  workflow_id: number;
  tasks: TaskDefinition[];
}

export interface WorkflowRun {
  id: number;
  workflow_id: number;
  triggered_by_user_id: number;
  created_at: string;
  definition_snapshot: unknown;
}

/** Run-scoped equivalent of WorkflowStatus (mirrors api/schemas.py's
 * RunStatusResponse) — the same six counts + is_complete/has_failures,
 * scoped to one run's materialized tasks. */
export interface RunStatus {
  run_id: number;
  total_tasks: number;
  pending: number;
  ready: number;
  running: number;
  succeeded: number;
  failed_terminal: number;
  skipped: number;
  is_complete: boolean;
  has_failures: boolean;
}

export function getWorkflowDefinition(workflowId: number): Promise<WorkflowDefinition> {
  return request<WorkflowDefinition>(`/workflows/${workflowId}/definition`);
}

export function saveWorkflowDefinition(
  workflowId: number,
  tasks: TaskDefinitionInput[],
  dependencies: TaskDefinitionDependencyInput[]
): Promise<WorkflowDefinition> {
  return request<WorkflowDefinition>(`/workflows/${workflowId}/definition`, {
    method: "PUT",
    body: JSON.stringify({ tasks, dependencies }),
  });
}

// ---------------------------------------------------------------------------
// Workflow runs — the "Run" action + execution monitoring. Each maps 1:1
// onto an existing route in api/routes.py:
//   POST   /workflows/{id}/runs            -> materialize definition -> run
//   GET    /workflows/{id}/runs            -> list a workflow's runs
//   GET    /workflows/{id}/runs/{rid}/status -> per-run state counts
//   GET    /workflows/{id}/runs/{rid}/tasks  -> that run's tasks
// A run is the only thing that creates dispatchable task rows; saving a
// definition never does.
// ---------------------------------------------------------------------------

export function createWorkflowRun(workflowId: number): Promise<WorkflowRun> {
  return request<WorkflowRun>(`/workflows/${workflowId}/runs`, { method: "POST" });
}

export function listWorkflowRuns(workflowId: number): Promise<WorkflowRun[]> {
  return request<WorkflowRun[]>(`/workflows/${workflowId}/runs`);
}

export function getRunStatus(workflowId: number, runId: number): Promise<RunStatus> {
  return request<RunStatus>(`/workflows/${workflowId}/runs/${runId}/status`);
}

export function listRunTasks(workflowId: number, runId: number): Promise<Task[]> {
  return request<Task[]>(`/workflows/${workflowId}/runs/${runId}/tasks`);
}

// ---------------------------------------------------------------------------
// Auth — one function per api/auth_routes.py route.
// ---------------------------------------------------------------------------

export function register(email: string, password: string): Promise<User> {
  return request<User>("/auth/register", {
    method: "POST",
    body: JSON.stringify({ email, password }),
  });
}

export function login(email: string, password: string): Promise<AuthResponse> {
  return request<AuthResponse>("/auth/login", {
    method: "POST",
    body: JSON.stringify({ email, password }),
  });
}

export function logout(): Promise<{ detail: string }> {
  return request<{ detail: string }>("/auth/logout", { method: "POST" });
}

export function getMe(): Promise<User> {
  return request<User>("/auth/me");
}

// ---------------------------------------------------------------------------
// One function per existing backend route. Nothing more.
// ---------------------------------------------------------------------------

export function listWorkflows(): Promise<Workflow[]> {
  return request<Workflow[]>("/workflows");
}

export function createWorkflow(name: string): Promise<Workflow> {
  return request<Workflow>("/workflows", {
    method: "POST",
    body: JSON.stringify({ name }),
  });
}

export function getWorkflow(workflowId: number): Promise<Workflow> {
  return request<Workflow>(`/workflows/${workflowId}`);
}

export function createTask(workflowId: number, input: CreateTaskInput): Promise<Task> {
  return request<Task>(`/workflows/${workflowId}/tasks`, {
    method: "POST",
    body: JSON.stringify(input),
  });
}

export function getTask(taskId: number): Promise<Task> {
  return request<Task>(`/tasks/${taskId}`);
}

export function listWorkflowTasks(workflowId: number): Promise<Task[]> {
  return request<Task[]>(`/workflows/${workflowId}/tasks`);
}

export function getWorkflowStatus(workflowId: number): Promise<WorkflowStatus> {
  return request<WorkflowStatus>(`/workflows/${workflowId}/status`);
}

export function listTaskTypes(): Promise<TaskTypeInfo[]> {
  return request<TaskTypeInfo[]>("/task-types");
}

export interface HealthStatus {
  status: string;
  detail?: string;
}

// GET /healthz — liveness only, no DB touch. GET /readyz — 503 if Postgres
// is unreachable. Both real, existing routes (api/health.py); request()
// already throws ApiError on a non-2xx, so a 503 /readyz surfaces the
// same way any other API error would.
export function getHealthz(): Promise<HealthStatus> {
  return request<HealthStatus>("/healthz");
}

export function getReadyz(): Promise<HealthStatus> {
  return request<HealthStatus>("/readyz");
}
