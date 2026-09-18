"""The ``ExecutionBinding`` seam — where instance-specific execution plugs in.

The core owns the *lifecycle*: claiming a run, binding the checkpointer,
supplying ``thread_id=run_id``, invoking or resuming the graph, and committing
the terminal transition. It deliberately owns nothing about WHICH graph runs or
what its input looks like, because that is the one part every consumer differs
on:

* Stromy resolves graphs from ``langgraph.json`` and hydrates a hosted contract
  into a real LangGraph runtime context.
* A client executor resolves its own single graph and builds a channel-shaped
  input from the inbound envelope.

A bare "graph resolver" callable is NOT sufficient to preserve the current
Stromy worker — it also needs the input payload (which may be a ``Command``
rather than a dict), the runtime context, and the terminal projection. All four
are therefore on the protocol, and the protocol is async because two of the four
are already async-shaped in the consumers.

Nothing in this module imports LangGraph at runtime; the types are ``TYPE_CHECKING``
only, so a facade installing the base package can still import ``binding`` for
its type annotations without acquiring the executor extra.
"""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import AbstractAsyncContextManager
from typing import Any, Protocol, TypeAlias, runtime_checkable

from .models import RunRecord, TerminalProjection

# LangGraph ships no type stubs and its generics are re-parameterised between
# minor releases, so importing the real classes here would (a) make the BASE
# install depend on the executor extra for type resolution and (b) pin us to one
# release's arity. The aliases keep the protocol's intent readable while staying
# honest that the payloads are opaque to the core — it only threads them through.
#: A ``langgraph.graph.state.CompiledStateGraph``.
CompiledStateGraph: TypeAlias = Any
#: A ``langgraph.types.Command``, used to resume a paused run.
Command: TypeAlias = Any
#: A ``langgraph.types.StateSnapshot`` read back from the checkpointer.
StateSnapshot: TypeAlias = Any


@runtime_checkable
class ExecutionBinding(Protocol):
    """What a consumer supplies so the core can execute its runs.

    Implementations must be side-effect-free with respect to run state: the core
    owns every registry transition. A binding that marks its own runs terminal
    will double-write and lose the lease guarantee.
    """

    async def resolve_graph(self, workflow: str) -> CompiledStateGraph:
        """Return the compiled graph for ``workflow``.

        Raise for an unknown workflow — the core records that as a run failure
        with the raised message, rather than guessing a default graph.
        """
        ...

    async def build_input(self, run: RunRecord) -> dict[str, Any] | Command:
        """Build the payload passed to the graph's first invocation.

        Return a ``Command`` to resume a paused run. The core never inspects the
        payload's contents; it only threads it through.
        """
        ...

    async def build_context(self, run: RunRecord) -> Any:
        """Build the LangGraph runtime context, or ``None`` for a context-free graph."""
        ...

    async def project_terminal(
        self, run: RunRecord, snapshot: StateSnapshot
    ) -> TerminalProjection:
        """Map a terminal graph snapshot to the durable outcome.

        Called only once the graph has genuinely finished (``snapshot.next == ()``).
        A pause is handled by the core and never reaches this method, so an
        implementation does not need to detect interrupts.

        This is also where a consumer publishes its declared exports, because it
        is the last point before the core commits the terminal transition:
        publishing here and returning ``artifacts_published=True`` means a run
        can never be ``completed`` while its outputs are missing. Raising instead
        leaves the run failed-and-retryable with its workspace intact.
        """
        ...


@runtime_checkable
class ConfiguredExecutionBinding(ExecutionBinding, Protocol):
    """An :class:`ExecutionBinding` that contributes to the *runnable* config.

    OPTIONAL, probed by attribute, same shape as
    :class:`ScopedExecutionBinding` — a binding without it is untouched.

    **Why this exists at all.** The core owns the runnable config it hands to
    ``astream``, and built it as ``{"configurable": {...}}`` and nothing else. But
    LangChain carries callbacks, metadata and tags at the config's TOP level, not
    under ``configurable``, so there was no channel a binding could put a callback
    handler on. That is not a gap with a workaround: ``build_context`` returns
    LangGraph's *runtime context*, a different parameter entirely, so a binding
    that returned ``{"callbacks": [...]}`` from it was handing the handler to the
    graph as state and the callback manager never saw it. It ran, it logged
    nothing, and nothing failed — measured 2026-09-09 on ``gmf-support-agent``
    (ORG-291): a completed run, ``egress delivered=1``, and zero traces.

    So tracing is the motivating case, but the seam is the runnable config in
    general — ``callbacks``, ``metadata``, ``tags``, ``run_name``.

    ``configurable`` is RESERVED and raising on it is the point rather than a
    nicety: it is where ``thread_id`` lives, and a binding that overwrote it would
    silently detach the run from its own checkpoint thread — resuming a different
    run's state, or none. A named refusal at the seam beats a corrupted
    checkpointer downstream.
    """

    async def build_invoke_config(self, run: RunRecord) -> Mapping[str, Any]:
        """Top-level runnable-config keys for this run's invocation.

        Return an empty mapping to contribute nothing. May not contain
        ``configurable`` (see the class docstring).
        """
        ...


@runtime_checkable
class ScopedExecutionBinding(ExecutionBinding, Protocol):
    """An :class:`ExecutionBinding` that also needs process state bound per run.

    OPTIONAL, and deliberately a separate protocol rather than a method on
    :class:`ExecutionBinding` with a default. Every existing consumer keeps
    working untouched because the runner probes for the attribute — a binding
    that does not define it gets a no-op scope, which is the correct behaviour
    and not a degraded one.

    The scope is entered BEFORE ``resolve_graph`` and exited in a ``finally``.
    Both halves are load-bearing, and for different reasons:

    * **Before resolution**, because a graph is resolved and compiled against
      whatever the process environment says at that moment. A binding that binds
      credentials after resolution has already let a module-level provider
      client capture the wrong ones.
    * **In a ``finally``**, because the interesting failure is the exception. A
      scope that only unwinds on success leaves whatever it bound in place for
      the rest of the process — and in the BYOK case (ORG-PLAN-206) what it bound
      is one client's provider key, sitting in an environment that a later
      operator-funded code path would happily read.

    Raise from ``__aenter__`` to fail the run before any graph work happens. The
    runner records the raised exception's ``.stage`` when it declares one (see
    :class:`~workflow_runtime_core.exceptions.StageFailure`), so a binding can
    say ``credentials`` and have a client-facing surface report where the run
    died without exposing why.
    """

    def execution_scope(self, run: RunRecord) -> AbstractAsyncContextManager[None]:
        """Bind per-run process state for the duration of the execution."""
        ...


@runtime_checkable
class LeaseRenewer(Protocol):
    """Keeps a claimed run's single-writer lease alive during a long execution.

    Separate from :class:`ExecutionBinding`, and optional, because it is a
    property of the *transport* rather than of the workflow. A run started by id
    (the ``--run-id`` lane) has no message to keep invisible and passes none; a
    run delivered on a queue must renew the registry lease and the message's
    invisibility **together**. Renewing only one is a silent single-writer
    violation: extend the invisibility alone and a crash strands the run until an
    operator notices, extend the registry lease alone and the message redelivers
    to a second runner while the first is still going.

    Implementations are async because the core renews from inside the same event
    loop that is driving the graph. A renewal that talks to a blocking driver
    (psycopg, the Azure SDK) must therefore hand off to a thread — otherwise it
    stalls the graph it is protecting.
    """

    #: Seconds between renewal attempts. Must be comfortably shorter than the
    #: lease itself: the core checks the graph and renews on this cadence, so a
    #: value close to the lease duration races the expiry it exists to prevent.
    interval_seconds: float

    async def renew(self) -> bool:
        """Extend the lease. ``False`` means it was lost and the run must stop."""
        ...
