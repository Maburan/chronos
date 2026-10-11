import { useEffect, useState } from "react";
import { listTaskTypes, type TaskTypeInfo } from "./api";

/**
 * GET /task-types, fetched once per mounted component. TASK_TYPE_METADATA
 * (worker/tasks/__init__.py) is the single source of truth this reads
 * from -- every icon/label/category/field shown anywhere in the frontend
 * comes from here, never a hardcoded per-type mapping, so a new task type
 * shows up correctly with zero frontend code changes.
 */
export function useTaskTypes(): TaskTypeInfo[] {
  const [taskTypes, setTaskTypes] = useState<TaskTypeInfo[]>([]);
  useEffect(() => {
    let cancelled = false;
    listTaskTypes()
      .then((types) => {
        if (!cancelled) setTaskTypes(types);
      })
      .catch(() => {
        if (!cancelled) setTaskTypes([]);
      });
    return () => {
      cancelled = true;
    };
  }, []);
  return taskTypes;
}

export function taskTypeInfo(taskTypes: TaskTypeInfo[], type: string): TaskTypeInfo | undefined {
  return taskTypes.find((t) => t.type === type);
}
