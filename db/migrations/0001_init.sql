-- Chronos persistence layer -- Phase 1 core schema.
--
-- Requires Postgres 13+ (native `GENERATED ALWAYS AS IDENTITY`; no other
-- version-sensitive features are used here -- Phase 1 uses BIGINT identity
-- keys throughout, per the approved reconciliation, not UUID).
--
-- outbox_events is intentionally NOT created here -- it is Phase 2 scope,
-- created in 0002_outbox.sql instead.
--
-- schema_migrations itself is NOT created by this file. It is bootstrapped
-- directly by db/migrate.py before any versioned migration is evaluated,
-- because the runner needs that table to exist before it can even check
-- which versioned migrations (this one included) have already been
-- applied -- a versioned migration file can't own its own prerequisite.

CREATE TYPE task_state AS ENUM (
    'PENDING', 'READY', 'RUNNING', 'SUCCEEDED', 'FAILED_TERMINAL', 'SKIPPED'
);

CREATE TABLE workflows (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name        TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE tasks (
    id               BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    workflow_id      BIGINT NOT NULL REFERENCES workflows(id) ON DELETE CASCADE,
    name             TEXT NOT NULL,
    task_type        TEXT NOT NULL,
    state            task_state NOT NULL,
    attempt_count    INT NOT NULL DEFAULT 0,
    max_attempts     INT NOT NULL DEFAULT 1,
    lease_owner      TEXT,
    lease_expires_at TIMESTAMPTZ,
    payload          JSONB,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- These mirror core/transitions.py's own TaskInstance.__post_init__
    -- invariants exactly, so a bug that bypasses the persistence layer
    -- (a hand-written migration, a bad hotfix query) still can't corrupt
    -- a row into a state the frozen module would never produce.
    CONSTRAINT lease_pair_consistent CHECK (
        (lease_owner IS NULL) = (lease_expires_at IS NULL)
    ),
    CONSTRAINT lease_requires_running CHECK (
        lease_owner IS NULL OR state = 'RUNNING'
    ),
    CONSTRAINT attempt_bounds CHECK (
        attempt_count >= 0 AND attempt_count <= max_attempts
    ),
    CONSTRAINT max_attempts_positive CHECK (max_attempts >= 1)
);

CREATE TABLE task_dependencies (
    task_id            BIGINT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    depends_on_task_id BIGINT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    PRIMARY KEY (task_id, depends_on_task_id),
    CONSTRAINT no_self_dependency CHECK (task_id <> depends_on_task_id)
);

-- Partial index carrying the claim query's whole cost: without it, every
-- claim attempt is a sequential scan over the entire tasks table.
CREATE INDEX idx_tasks_ready_claim
    ON tasks (created_at) WHERE state = 'READY';

-- Partial index for the (not-yet-implemented) reaper's expired-lease sweep.
CREATE INDEX idx_tasks_running_lease
    ON tasks (lease_expires_at) WHERE state = 'RUNNING';

CREATE INDEX idx_tasks_workflow_id ON tasks (workflow_id);
CREATE INDEX idx_tasks_state ON tasks (state);

-- Fan-out lookup: "who depends on me" -- used by both the PENDING->READY
-- propagation pass and the SKIPPED cascade.
CREATE INDEX idx_task_deps_depends_on
    ON task_dependencies (depends_on_task_id);
