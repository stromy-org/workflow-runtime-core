"""Durable ingress — the one transaction that makes acknowledgement safe.

:func:`submit_event` writes the inbox row, the run and its pending launch in a
SINGLE transaction. Only that commit permits a broker acknowledgement, and the
ordering is what makes the whole pipeline recoverable:

* crash **before** the commit → nothing exists, the delivery is unacknowledged,
  the broker redelivers, and the retry is indistinguishable from a first attempt;
* crash **after** the commit but before the ack → the row exists, the broker
  redelivers, and the unique key resolves the duplicate to the SAME ``run_id``
  instead of starting a second run.

There is no ordering that avoids the second case — an acknowledgement and a
database commit cannot be made atomic across two systems — so the design makes
it harmless rather than pretending to prevent it. That is the whole reason the
inbox is keyed on the *channel's* message id.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any

import psycopg

from .. import registry
from ..models import RunRecord
from ..registry import DbConnection
from .envelope import Envelope


@dataclass(frozen=True)
class SubmitResult:
    """Outcome of an ingress submission.

    ``duplicate`` is a normal, expected outcome, not an error: it is what a
    redelivered message looks like once the system is working correctly. Callers
    acknowledge the delivery either way.
    """

    run_id: str
    inbox_id: str
    duplicate: bool


def _existing(conn: DbConnection, envelope: Envelope) -> SubmitResult | None:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT inbox_id, run_id FROM event_inbox
             WHERE service_namespace = %s AND source = %s AND source_message_id = %s
            """,
            (envelope.service_namespace, envelope.source, envelope.source_message_id),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return SubmitResult(
        run_id=str(row["run_id"]), inbox_id=str(row["inbox_id"]), duplicate=True
    )


def submit_event(
    conn: DbConnection,
    envelope: Envelope,
    *,
    launcher: str,
    params_hash: str,
    config: dict[str, Any] | None = None,
    client_slug: str | None = None,
    image_tag: str | None = None,
) -> SubmitResult:
    """Record an inbound event, its run and its pending launch atomically.

    Returns the existing ``run_id`` for a duplicate ``(service_namespace,
    source, source_message_id)``, without creating anything.

    The caller MUST NOT acknowledge the delivery until this function has
    returned *and* the surrounding transaction has committed. Acknowledging on
    return alone re-introduces the lost-message window this exists to close.
    """
    envelope.validate()

    found = _existing(conn, envelope)
    if found is not None:
        return found

    # The racing-INSERT savepoint: two ingress replicas can both miss the SELECT
    # above and both proceed. The loser's INSERT hits the unique index; rolling
    # back to the savepoint keeps the OUTER transaction usable (a bare failed
    # statement would poison it) so we can re-read the winner's row and return
    # the same run_id it created.
    inbox_id = str(uuid.uuid4())
    try:
        with conn.transaction():
            run = registry.create_run(
                conn,
                workflow=envelope.workflow,
                config=config or {},
                client_slug=client_slug,
                image_tag=image_tag,
            )
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO event_inbox (
                        inbox_id, service_namespace, source, source_message_id,
                        run_id, envelope_json
                    ) VALUES (%s, %s, %s, %s, %s, %s)
                    """,
                    (
                        inbox_id,
                        envelope.service_namespace,
                        envelope.source,
                        envelope.source_message_id,
                        run.run_id,
                        json.dumps(envelope.as_dict(), default=str),
                    ),
                )
                cur.execute(
                    """
                    INSERT INTO run_launches (run_id, launcher, state, params_hash)
                    VALUES (%s, %s, 'pending', %s)
                    ON CONFLICT (run_id) DO NOTHING
                    """,
                    (run.run_id, launcher, params_hash),
                )
    except psycopg.errors.UniqueViolation:
        raced = _existing(conn, envelope)
        if raced is None:  # pragma: no cover - would mean the index vanished
            raise
        return raced

    return SubmitResult(run_id=run.run_id, inbox_id=inbox_id, duplicate=False)


def get_envelope(conn: DbConnection, run_id: str) -> Envelope | None:
    """Re-read the envelope that created a run.

    The runner's binding needs this to build graph input on a RECOVERY path,
    where the original in-memory envelope is long gone with the process that
    held it. Storing it is what makes a run reconstructible from the database
    alone.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT envelope_json FROM event_inbox WHERE run_id = %s LIMIT 1",
            (run_id,),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return Envelope.from_dict(row["envelope_json"])


def run_for_source(
    conn: DbConnection, *, service_namespace: str, source: str, source_message_id: str
) -> RunRecord | None:
    """The run a given channel message produced, if any."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT run_id FROM event_inbox
             WHERE service_namespace = %s AND source = %s AND source_message_id = %s
            """,
            (service_namespace, source, source_message_id),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return registry.get_run(conn, str(row["run_id"]))


def purge_inbox(
    conn: DbConnection,
    *,
    service_namespace: str,
    older_than_days: int = 30,
    dry_run: bool = False,
) -> int:
    """Delete inbox rows for terminal runs older than the retention window.

    Scoped to terminal runs on purpose: age alone is not a safe predicate,
    because a paused run legitimately waits for a human far longer than the
    retention window and still needs its envelope to resume.

    ``dry_run`` counts what would go using the SAME predicate rather than a
    re-typed copy of it. A preview that can disagree with the deletion it
    previews is worse than no preview.
    """
    from ..models import TERMINAL_STATUS_VALUES

    # One WHERE clause, used verbatim by both the count and the delete. Written
    # with EXISTS rather than a join precisely so the two statements can share
    # it character-for-character — a DELETE ... USING and a SELECT ... JOIN
    # would need different text, and a preview that can drift from the deletion
    # it previews is worse than no preview.
    where = """
         WHERE service_namespace = %s
           AND received_at < now() - make_interval(days => %s)
           AND EXISTS (
                 SELECT 1 FROM runs r
                  WHERE r.run_id = event_inbox.run_id
                    AND r.status = ANY(%s)
               )
    """
    params = (service_namespace, older_than_days, list(TERMINAL_STATUS_VALUES))

    with conn.cursor() as cur:
        if dry_run:
            # noqa on the concatenation itself: `where` is a module-local
            # literal and every value is bound as a parameter.
            cur.execute(
                "SELECT count(*) AS n FROM event_inbox" + where,  # noqa: S608
                params,
            )
            row = cur.fetchone()
            return 0 if row is None else int(row["n"])
        cur.execute(
            "DELETE FROM event_inbox" + where,  # noqa: S608
            params,
        )
        return cur.rowcount
