import type { TaskState } from "./api";

/**
 * Single source of truth for how a TaskState maps to a badge class and
 * a human icon/label. Every component that renders state (TaskList,
 * TaskDetailPanel, DependencyGraph, MetricCards) reads from here instead
 * of re-declaring its own switch — keeps the six states' visual meaning
 * consistent everywhere they appear.
 */
export const STATE_BADGE_CLASS: Record<TaskState, string> = {
  PENDING: "badge-pending",
  READY: "badge-ready",
  RUNNING: "badge-running",
  SUCCEEDED: "badge-succeeded",
  FAILED_TERMINAL: "badge-failed",
  SKIPPED: "badge-skipped",
};

export const STATE_COLOR_VAR: Record<TaskState, string> = {
  PENDING: "var(--c-pending)",
  READY: "var(--c-ready)",
  RUNNING: "var(--c-running)",
  SUCCEEDED: "var(--c-succeeded)",
  FAILED_TERMINAL: "var(--c-failed)",
  SKIPPED: "var(--c-skipped)",
};

// The legal lifecycle path a task can be on, per core/transitions.py.
// PENDING only appears for tasks with unmet dependencies; every task
// that has no dependencies starts at READY directly. Used by
// TaskDetailPanel's stepper to show "how far along" a task's *current*
// state is on its actual legal path — never a fabricated event log.
export const LIFECYCLE_SUCCESS_PATH: TaskState[] = ["PENDING", "READY", "RUNNING", "SUCCEEDED"];
export const LIFECYCLE_FAILURE_PATH: TaskState[] = ["PENDING", "READY", "RUNNING", "FAILED_TERMINAL"];
export const LIFECYCLE_SKIPPED_PATH: TaskState[] = ["PENDING", "SKIPPED"];

export function lifecycleFor(state: TaskState): TaskState[] {
  if (state === "SKIPPED") return LIFECYCLE_SKIPPED_PATH;
  if (state === "FAILED_TERMINAL") return LIFECYCLE_FAILURE_PATH;
  return LIFECYCLE_SUCCESS_PATH;
}
