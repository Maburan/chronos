import { listRunTasks, listWorkflowRuns, type Task, type Workflow } from "./api";
import { countStates, type StateCounts } from "./execution";
import { deriveWorkflowStatus, type WorkflowDisplayStatus } from "./workflowStatus";

/**
 * A workflow plus its *latest run's* real tasks — the data a dashboard /
 * list card needs to show meaningful execution structure (a MiniDag strip
 * and a live status), rather than just a name and a chip. A workflow with
 * no runs yet resolves to an empty task list (a draft strip).
 */
export interface WorkflowOverview {
  workflow: Workflow;
  latestRunId: number | null;
  tasks: Task[];
  counts: StateCounts;
  display: WorkflowDisplayStatus;
}

export async function fetchWorkflowOverview(workflow: Workflow): Promise<WorkflowOverview> {
  let latestRunId: number | null = null;
  let tasks: Task[] = [];
  try {
    const runs = await listWorkflowRuns(workflow.id);
    if (runs.length > 0) {
      latestRunId = [...runs].sort((a, b) => b.id - a.id)[0].id;
      tasks = await listRunTasks(workflow.id, latestRunId);
    }
  } catch {
    // leave as a draft-shaped overview on any per-workflow read failure
  }
  const counts = countStates(tasks);
  const display = deriveWorkflowStatus({
    total_tasks: counts.total,
    pending: counts.pending,
    ready: counts.ready,
    running: counts.running,
    succeeded: counts.succeeded,
    failed_terminal: counts.failed_terminal,
    skipped: counts.skipped,
    is_complete: counts.total > 0 && counts.settled === counts.total,
    has_failures: counts.failed_terminal > 0,
  });
  return { workflow, latestRunId, tasks, counts, display };
}

export function fetchWorkflowOverviews(workflows: Workflow[]): Promise<WorkflowOverview[]> {
  return Promise.all(workflows.map(fetchWorkflowOverview));
}
