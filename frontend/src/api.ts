/**
 * Typed client for the Chronos HTTP API.
 *
 * This file intentionally does nothing beyond: build a request, call
 * fetch, parse JSON, throw on a non-2xx status. It does not retry, cache,
 * or infer anything about task state that the backend didn't already
 * compute — every field here mirrors api/schemas.py exactly, and every
 * function here maps 1:1 onto one route in api/routes.py. If a field
 * doesn't exist on the backend response, it must not appear here.
 *
 * There is no "list workflows" or "get task dependencies" endpoint on the
 * backend (confirmed by reading api/routes.py directly) — do not add
 * client functions here that imply they exist.
 */

const API_BASE_URL = import.meta.env.VITE_CHRONOS_API_URL ?? "http://localhost:8000";

export class ApiError extends Error {
  status: number;
  detail: unknown;

  constructor(status: number, detail: unknown) {
    super(`Chronos API error ${status}: ${JSON.stringify(detail)}`);
    this.status = status;
    this.detail = detail;
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${API_BASE_URL}${path}`, {
    ...init,
    headers: {
      "Content-Type": "application/json",
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

export interface Workflow {
  id: number;
  name: string;
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

// ---------------------------------------------------------------------------
// One function per existing backend route. Nothing more.
// ---------------------------------------------------------------------------

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
