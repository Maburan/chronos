-- Chronos persistence layer -- Definition vs Run.
--
-- Separates the reusable workflow *design* (task_definitions /
-- task_definition_dependencies, edited by the Builder's Save action) from
-- one executable *run* of it (workflow_runs). `workflows`/`tasks`/
-- `task_dependencies` (0001_init.sql) are UNCHANGED in shape and meaning:
-- `tasks`/`task_dependencies` remain exactly the live execution rows the
-- existing engine (core/transitions.py, the transactional outbox,
-- RabbitMQ, workers, leases/heartbeats, the Reaper) already operates on.
-- Nothing below this line changes what any of those components do or how
-- they read `tasks`/`task_dependencies` -- this migration only adds two
-- nullable traceability columns to `tasks` (see below) plus new tables
-- above and alongside them.
--
-- Immutability of run history: a run's meaning is fixed at the moment
-- `workflow_runs` is inserted and never re-derived from `task_definitions`
-- afterward. `workflow_runs.definition_snapshot` stores a JSONB copy of
-- the exact task/dependency graph and configuration that was materialized
-- into real `tasks`/`task_dependencies` rows for that run. Editing or
-- deleting a `task_definitions` row later cannot change what an existing
-- run represents -- the snapshot, and the materialized `tasks` rows
-- themselves (which never change task_type/payload/name after insert,
-- same as today), are the durable record. `tasks.task_definition_id` is
-- kept as a best-effort traceability link back to the definition-time row
-- ("which builder node did this come from") and goes NULL
-- (ON DELETE SET NULL) if that definition row is later deleted -- it is
-- never the run's source of truth, only the snapshot is.

CREATE TABLE task_definitions (
    id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    workflow_id  BIGINT NOT NULL REFERENCES workflows(id) ON DELETE CASCADE,
    name         TEXT NOT NULL,
    task_type    TEXT NOT NULL,
    max_attempts INT NOT NULL DEFAULT 1,
    payload      JSONB,
    position_x   DOUBLE PRECISION,
    position_y   DOUBLE PRECISION,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT td_max_attempts_positive CHECK (max_attempts >= 1)
);

-- `branch` is reserved for the isolated If/Else branching phase (not yet
-- interpreted by anything in this migration or by run materialization --
-- Phase 2 treats every edge as a plain AND-dependency, exactly like
-- task_dependencies today). Storing it now means the Builder can already
-- persist a branch label on an edge without a further schema change once
-- the engine actually learns to interpret it.
CREATE TABLE task_definition_dependencies (
    task_definition_id            BIGINT NOT NULL REFERENCES task_definitions(id) ON DELETE CASCADE,
    depends_on_task_definition_id BIGINT NOT NULL REFERENCES task_definitions(id) ON DELETE CASCADE,
    branch                        TEXT,
    PRIMARY KEY (task_definition_id, depends_on_task_definition_id),
    CONSTRAINT td_no_self_dependency CHECK (task_definition_id <> depends_on_task_definition_id)
);

CREATE INDEX idx_task_definitions_workflow_id ON task_definitions (workflow_id);
CREATE INDEX idx_task_def_deps_depends_on
    ON task_definition_dependencies (depends_on_task_definition_id);

-- One row per Run action. No `status` column deliberately -- exactly like
-- today's WorkflowStatusResponse, a run's rollup status is computed on
-- read from the state of its materialized `tasks` rows
-- (queries.SELECT_RUN_TASK_STATE_COUNTS), never stored and kept in sync
-- separately; that would be a second, potentially-drifting source of
-- truth for something PostgreSQL can already answer directly.
CREATE TABLE workflow_runs (
    id                   BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    workflow_id          BIGINT NOT NULL REFERENCES workflows(id) ON DELETE CASCADE,
    triggered_by_user_id BIGINT NOT NULL REFERENCES users(id),
    definition_snapshot  JSONB NOT NULL,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_workflow_runs_workflow_id ON workflow_runs (workflow_id);

-- Both nullable: existing direct `persistence.create_task` callers (every
-- pre-Phase-2 test and code path) never set either column, and continue
-- to produce rows exactly as before -- a "definition-less" task, same as
-- today. Only run-materialized tasks get both populated.
ALTER TABLE tasks ADD COLUMN workflow_run_id BIGINT REFERENCES workflow_runs(id) ON DELETE CASCADE;
ALTER TABLE tasks ADD COLUMN task_definition_id BIGINT REFERENCES task_definitions(id) ON DELETE SET NULL;

CREATE INDEX idx_tasks_workflow_run_id ON tasks (workflow_run_id) WHERE workflow_run_id IS NOT NULL;
