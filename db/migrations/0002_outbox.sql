-- Chronos persistence layer -- Phase 2: transactional outbox.
--
-- outbox_events records a durable, transactionally-consistent fact
-- ("this task became READY") alongside the task-state write that produced
-- it, so a relay (Phase 3, not implemented here) can later publish it to
-- RabbitMQ without ever risking the dual-write problem: the task-state
-- transition and its outbox row are written in one PostgreSQL transaction,
-- or neither is.
--
-- Per the approved reconciliation, this uses BIGINT identity (not UUID)
-- for consistency with workflows.id / tasks.id.
--
-- The row is a dispatch hint, not authoritative task state -- the future
-- worker uses task_id to perform its own targeted, idempotent PostgreSQL
-- claim (state = READY). At-least-once delivery is the deliberate design;
-- no dedup/processed-message table is introduced here.

CREATE TABLE outbox_events (
    id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    task_id       BIGINT NOT NULL REFERENCES tasks(id),
    workflow_id   BIGINT NOT NULL REFERENCES workflows(id),
    event_type    TEXT NOT NULL DEFAULT 'TASK_READY',
    payload       JSONB NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    published_at  TIMESTAMPTZ
);

-- Carries the future relay's whole read cost: "give me the oldest
-- unpublished events" without a sequential scan over every row ever
-- written, published or not. Mirrors idx_tasks_ready_claim's shape
-- (partial index scoped to the one predicate the read query actually
-- uses).
CREATE INDEX idx_outbox_unpublished
    ON outbox_events (created_at) WHERE published_at IS NULL;
