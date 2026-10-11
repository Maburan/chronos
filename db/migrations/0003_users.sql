-- Chronos persistence layer -- Phase (multi-user): accounts + workflow
-- ownership.
--
-- Adds users and ties every workflow to exactly one owning user. Ownership
-- is enforced at the workflow level only -- tasks/task_dependencies/
-- outbox_events already join through workflow_id for every read the API
-- performs, so they don't need their own user_id column.
--
-- Password hashing and JWT issuance/verification happen entirely outside
-- PostgreSQL (see auth/passwords.py, auth/tokens.py) -- this table only
-- ever stores the resulting bcrypt hash, never a plaintext password.

CREATE TABLE users (
    id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    email         TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT users_email_unique UNIQUE (email)
);

-- NOT NULL from the start: this is a pre-launch schema change with no
-- existing production workflow rows to backfill (see HANDOVER.md -- the
-- project has never shipped without auth). A future migration that needs
-- to add a NOT NULL column to a table with live data would need a
-- backfill-then-constrain sequence instead; that isn't needed here.
ALTER TABLE workflows ADD COLUMN user_id BIGINT NOT NULL REFERENCES users(id);

CREATE INDEX idx_workflows_user_id ON workflows (user_id);
