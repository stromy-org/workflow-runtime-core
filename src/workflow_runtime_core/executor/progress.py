"""Central progress + heartbeat from the graph's own event stream.

A hosted run can occupy a container for hours with nothing to show for it: until
this existed, ``run_status`` reported ``running`` from the first second to the
last, and the only way to tell a working run from a wedged one was to wait for it
to end. The graph already announces every node it finishes; this turns that
stream into two durable facts on the run row — *where it is* and *that it is still
alive* — written centrally so both consumers get them from one implementation.

Three properties, each of which is a decision rather than an implementation
detail:

**Rate-limited by time, bounded by node count.** A well-behaved graph completes
tens of nodes, so nearly every event is worth a round trip. The limit exists for
the other shape: a fan-out over five hundred documents, which would otherwise
write five hundred rows nobody reads. Suppressed events are not lost — the
counter keeps moving and the next admitted write carries it — and
:meth:`flush` forces the last one out, so the final state is always durable even
if it arrived inside a suppressed window.

**Best-effort, never fatal.** Progress is telemetry about a run, not part of it.
A registry that cannot record it (a v1 database inside the expansion window, most
obviously — ``progress_json`` does not exist there) must not turn a healthy
multi-hour run into a failure. So the first refusal disables the recorder and says
so once; the graph continues.

**Node names only.** What goes in the payload is the graph's own node identifiers
— static, org-authored labels — plus a count and a timestamp. Never the state
delta the node returned: that is the client's actual content, and
``progress_json`` is read back by :meth:`RunRecord.public`, which is a
client-facing surface.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Mapping
from typing import Any

from .. import registry
from ..exceptions import RegistryError
from ..models import utcnow

logger = logging.getLogger(__name__)

#: Minimum seconds between two durable progress writes for one run.
DEFAULT_PROGRESS_INTERVAL_SECONDS = 15.0

#: The rate limit's clock, named so it can be advanced in a test without freezing
#: ``time.monotonic`` for the whole process — the event loop reads that same
#: function, and a test that patches it globally stops asyncio's own timers.
#: Monotonic on purpose: an NTP correction must not retroactively open or close
#: the window.
_monotonic = time.monotonic


def node_names(chunk: Mapping[str, Any]) -> list[str]:
    """Node names in one ``stream_mode="updates"`` chunk.

    A chunk maps each node that just finished to its state delta, so one chunk can
    name several nodes when a superstep ran them in parallel. Keys beginning with
    ``__`` are LangGraph's own channels (``__interrupt__`` is the one that matters
    here) and are not nodes.
    """
    return [key for key in chunk if not key.startswith("__")]


class ProgressRecorder:
    """Accumulates node events for one run and persists them, rate-limited."""

    def __init__(
        self,
        run_id: str,
        *,
        dsn: str | None = None,
        min_interval_seconds: float = DEFAULT_PROGRESS_INTERVAL_SECONDS,
    ) -> None:
        self.run_id = run_id
        self._dsn = dsn
        self._interval = max(min_interval_seconds, 0.0)
        self._nodes_completed = 0
        self._last_node: str | None = None
        self._last_write: float | None = None
        self._unwritten = False
        self._disabled = False

    @property
    def nodes_completed(self) -> int:
        return self._nodes_completed

    @property
    def last_node(self) -> str | None:
        return self._last_node

    def snapshot(self) -> dict[str, Any]:
        """The payload written to ``runs.progress_json``."""
        return {
            "node": self._last_node,
            "nodes_completed": self._nodes_completed,
            "at": utcnow().isoformat(),
        }

    async def observe(self, chunk: Mapping[str, Any]) -> None:
        """Count one stream chunk, writing if the rate limit allows."""
        names = node_names(chunk)
        if not names:
            return
        self._nodes_completed += len(names)
        self._last_node = names[-1]
        self._unwritten = True
        # The FIRST event always writes (``_last_write is None``): a client watching
        # a run should see it move as soon as it moves, and making them wait out one
        # full interval for the first sign of life defeats the point.
        now = _monotonic()
        if self._last_write is None or now - self._last_write >= self._interval:
            await self._write()

    async def flush(self) -> None:
        """Force the latest snapshot out, regardless of the rate limit.

        Called once when the stream ends — including when it ended badly, so a
        failed run still records the node it died at. Cheap and idempotent: with
        nothing new since the last write it does nothing.
        """
        if self._unwritten:
            await self._write()

    async def _write(self) -> None:
        if self._disabled:
            return
        try:
            await asyncio.to_thread(self._write_blocking, self.snapshot())
        except RegistryError as exc:
            # Includes SchemaVersionMismatch on a v1 registry, which is the
            # expected case during the expansion window rather than an incident.
            self._disabled = True
            logger.info(
                "run %s: progress will not be recorded (%s); the run continues",
                self.run_id,
                exc,
            )
            return
        except Exception as exc:  # noqa: BLE001 - telemetry must not fail a run
            self._disabled = True
            logger.warning(
                "run %s: progress recording disabled after an unexpected error: %s",
                self.run_id,
                exc,
            )
            return
        self._last_write = _monotonic()
        self._unwritten = False

    def _write_blocking(self, payload: dict[str, Any]) -> None:
        with registry.connect(self._dsn) as conn:
            registry.record_progress(conn, self.run_id, payload)
