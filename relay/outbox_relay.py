"""The outbox relay: the only bridge from PostgreSQL's transactional
outbox to RabbitMQ.

PostgreSQL
    |
    | unpublished outbox_events
    v
Outbox Relay   <-- this module
    |
    | publisher confirms
    v
RabbitMQ

Nothing here touches task state. The relay reads ``outbox_events`` rows
that ``db/persistence.py`` already committed (see its module docstring for
the four READY-producing paths + ``mark_ready`` that write them, all in
the same transaction as the state change) and republishes each as a
dispatch hint. It never writes to ``tasks``, never calls
``core/transitions.py``, and never decides whether a task is claimable --
that remains PostgreSQL's job alone, enforced later by a worker's own
targeted claim (built in ``worker/task_worker.py``, not here).

Delivery is intentionally **at-least-once**: ``published_at`` is set only
after a publisher confirm comes back, and a crash between "broker
acked the publish" and "UPDATE committed" simply means that event gets
picked up and republished on a later pass. A worker receiving the same
dispatch hint twice is harmless -- only one of the two claim attempts can
ever succeed, since PostgreSQL is authoritative and claiming is
conditional (see ``queries.CLAIM_READY_TASK``). This module does not
attempt exactly-once delivery and does not introduce a deduplication
table; see CHRONOS_IMPLEMENTATION_HANDOFF.md section 8/15 for why that's
deliberate, not a gap.

Retry/backoff policy lives entirely in :meth:`OutboxRelay.run_forever`:
a single place, simple exponential backoff, no distributed coordination or
leader election. Nothing here assumes only one relay process is ever
running -- if two are (e.g. during a deploy), both attempting to mark the
same event published is safe, because ``MARK_OUTBOX_EVENT_PUBLISHED`` is
itself conditional (``WHERE published_at IS NULL``) and duplicate
publication is explicitly acceptable.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Optional

from psycopg_pool import ConnectionPool

from db import queries
from mq.config import RelayConfig
from mq.connection import ConfirmedPublisher
from mq.topology import routing_key_for_task_type

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RelayIterationResult:
    """What one ``run_once`` pass did, for both ``run_forever``'s backoff
    decision and tests -- never used to decide anything about task state."""

    fetched: int
    published: int
    errors: int


class OutboxRelay:
    """Polls ``outbox_events`` for unpublished rows and republishes each to
    RabbitMQ, marking it published only once the broker has confirmed it.

    ``publisher`` is any ``ConfirmedPublisher`` (see mq/connection.py) --
    tests inject a fake, production code injects a
    ``mq.connection.RabbitMQPublisher``. The relay itself never imports
    pika.
    """

    def __init__(
        self,
        pool: ConnectionPool,
        publisher: ConfirmedPublisher,
        *,
        config: Optional[RelayConfig] = None,
    ) -> None:
        self._pool = pool
        self._publisher = publisher
        self._config = config or RelayConfig()

    # -- one polling pass -----------------------------------------------

    def run_once(self) -> RelayIterationResult:
        """Fetch one bounded batch of unpublished events (oldest
        ``created_at`` first, preserving ``SELECT_UNPUBLISHED_OUTBOX_EVENTS``'s
        ordering) and attempt to publish+confirm each in turn.

        A publish/confirm failure on one event never aborts the batch --
        each event is independent, so the loop always continues to the
        next row. Connection-level failures on the publisher side are
        equally "leave this row unpublished and try the next one"; there's
        no special-casing here, because a broken publisher will simply
        keep failing to confirm for the rest of the batch, and
        ``run_forever`` backs off between polling passes regardless of
        which failure mode caused it.
        """
        events = self._fetch_unpublished_batch()
        published = 0
        errors = 0

        for event in events:
            try:
                confirmed = self._publish_event(event)
            except Exception:  # noqa: BLE001 - any publisher failure is "not confirmed"
                logger.warning(
                    "outbox relay: publish failed for event_id=%s, leaving unpublished",
                    event["id"],
                    exc_info=True,
                )
                errors += 1
                continue

            if not confirmed:
                errors += 1
                continue

            # Correlation logging only -- event_id is the one ID
            # already threaded through the dispatch hint end-to-end (see
            # worker/task_worker.py's matching log line), so logging it
            # here at the moment of confirmed publish is what actually
            # lets a relay log line and a worker log line for the same
            # event be tied together. Purely additive: does not affect
            # whether this event counts as published/errored below.
            logger.info(
                "outbox relay: confirmed publish event_id=%s task_id=%s task_type=%s",
                event["id"],
                event["task_id"],
                event["payload"]["task_type"],
            )

            # The message has already been confirmed by the broker at this
            # point -- a failure marking it published (e.g. the database
            # connection itself drops) must never be reported as success.
            # It also must never raise out of run_once: this is the same
            # "leave it unpublished, count it as an error, move on to the
            # next event" handling as a publish failure, just on the DB
            # side of the same operation. A duplicate publish next pass is
            # the accepted at-least-once outcome, not a bug.
            try:
                marked = self._mark_published(event["id"])
            except Exception:  # noqa: BLE001 - never let this look like a success
                logger.warning(
                    "outbox relay: failed to mark event_id=%s published after "
                    "confirmed publish; will be retried",
                    event["id"],
                    exc_info=True,
                )
                errors += 1
                continue

            if marked:
                published += 1
            # else: already marked published by a concurrent relay pass --
            # not an error, just not this pass's win. Duplicate publication
            # already happened above; that's acceptable at-least-once
            # behavior, not a bug to guard against here.

        return RelayIterationResult(
            fetched=len(events), published=published, errors=errors
        )

    def _fetch_unpublished_batch(self) -> list[dict]:
        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    queries.SELECT_UNPUBLISHED_OUTBOX_EVENTS,
                    {"batch_size": self._config.batch_size},
                )
                return cur.fetchall()

    def _publish_event(self, event: dict) -> bool:
        task_type = event["payload"]["task_type"]
        routing_key = routing_key_for_task_type(task_type)
        message = {
            "event_id": event["id"],
            "task_id": event["task_id"],
            "workflow_id": event["workflow_id"],
            "task_type": task_type,
        }
        body = json.dumps(message).encode("utf-8")
        return self._publisher.publish(routing_key=routing_key, body=body)

    def _mark_published(self, event_id: int) -> bool:
        """Marks the event published, but ONLY because ``_publish_event``
        already returned ``True`` (a confirmed publish) for this exact
        event_id -- this method is never called speculatively. Returns
        ``False`` (not an error) if the row was already published by the
        time this runs, since ``MARK_OUTBOX_EVENT_PUBLISHED`` is
        conditional on ``published_at IS NULL``. Any exception here (e.g.
        the database connection itself failing) propagates up to
        ``run_once``'s per-event try/except, which counts it as an error
        and leaves the row exactly as it was -- it never reports a false
        success.
        """
        with self._pool.connection() as conn:
            with conn.transaction():
                with conn.cursor() as cur:
                    cur.execute(
                        queries.MARK_OUTBOX_EVENT_PUBLISHED, {"event_id": event_id}
                    )
                    row = cur.fetchone()
        return row is not None

    # -- continuous operation --------------------------------------------

    def run_forever(self, *, stop_check=lambda: False, on_iteration=None) -> None:
        """Poll indefinitely until ``stop_check()`` returns ``True``.

        Simple deterministic exponential backoff, doubling from
        ``initial_backoff_seconds`` up to ``max_backoff_seconds`` on any
        pass that reported an error, reset to the initial value the moment
        a pass reports zero errors. An empty-but-error-free pass (no work
        to do) sleeps for the ordinary ``poll_interval_seconds`` instead --
        this is not a failure state and must not accumulate backoff.
        No distributed coordination, no leader election: multiple relay
        instances may run this loop concurrently and safely (see module
        docstring).

        ``on_iteration``, if given, is called once per *completed*
        iteration (i.e. one that produced a ``RelayIterationResult`` --
        never called for a pass where ``run_once`` itself raised, since
        there's no result to hand it), purely for lightweight operational
        logging in the entrypoint (``relay/__main__.py``) -- this method
        never calls it itself beyond invoking the callback; no metrics
        backend, no new infrastructure. Defaults to ``None`` so every
        existing caller's
        behavior is unchanged.
        """
        backoff = self._config.initial_backoff_seconds

        while not stop_check():
            try:
                result = self.run_once()
            except Exception:  # noqa: BLE001 - e.g. the DB itself is unreachable
                logger.warning("outbox relay: polling pass failed", exc_info=True)
                time.sleep(backoff)
                backoff = min(backoff * 2, self._config.max_backoff_seconds)
                continue

            if on_iteration is not None:
                on_iteration(result)

            if result.errors > 0:
                time.sleep(backoff)
                backoff = min(backoff * 2, self._config.max_backoff_seconds)
            else:
                backoff = self._config.initial_backoff_seconds
                time.sleep(self._config.poll_interval_seconds)
