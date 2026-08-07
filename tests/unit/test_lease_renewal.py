"""The lease-renewal loop, in isolation from Postgres and LangGraph.

``_with_lease_renewal`` is the only place in the core where a *second writer* is
actively prevented while work is in flight, so its two outcomes are pinned here
rather than left to the integration path: the invocation's result survives an
arbitrary number of renewals, and a lost lease cancels the invocation **before**
:class:`LeaseLost` escapes.
"""

from __future__ import annotations

import asyncio

import pytest

from workflow_runtime_core.exceptions import LeaseLost
from workflow_runtime_core.executor.runner import _with_lease_renewal


class _Renewer:
    """Grants ``grants`` renewals, then reports the lease lost."""

    def __init__(self, *, grants: int, interval_seconds: float = 0.01) -> None:
        self.interval_seconds = interval_seconds
        self._grants = grants
        self.calls = 0

    async def renew(self) -> bool:
        self.calls += 1
        return self.calls <= self._grants


@pytest.mark.unit
async def test_a_short_invocation_never_needs_a_renewal() -> None:
    async def _work() -> str:
        return "done"

    renewer = _Renewer(grants=0, interval_seconds=5)
    assert await _with_lease_renewal(_work(), renewer, run_id="r") == "done"
    assert renewer.calls == 0


@pytest.mark.unit
async def test_a_long_invocation_is_renewed_until_it_finishes() -> None:
    """The renewal must not stop at the first grant: a multi-hour node needs the
    lease extended repeatedly, and a loop that renewed once would expire mid-run."""

    async def _work() -> str:
        await asyncio.sleep(0.12)
        return "done"

    renewer = _Renewer(grants=100, interval_seconds=0.01)
    assert await _with_lease_renewal(_work(), renewer, run_id="r") == "done"
    assert renewer.calls >= 3


@pytest.mark.unit
async def test_a_lost_lease_cancels_the_invocation_before_raising() -> None:
    """Cancel-then-await, not raise-then-hope. If ``LeaseLost`` escaped while the
    graph was still running, this process would keep writing checkpoints while
    the replacement runner writes its own — the exact interleaving the lease
    exists to prevent."""
    observed: dict[str, bool] = {"cancelled": False, "finished": False}

    async def _work() -> str:
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            observed["cancelled"] = True
            raise
        observed["finished"] = True  # pragma: no cover - the sleep is cancelled
        return "done"

    renewer = _Renewer(grants=1, interval_seconds=0.01)
    with pytest.raises(LeaseLost, match="lease for run r-9 was lost"):
        await _with_lease_renewal(_work(), renewer, run_id="r-9")

    assert observed["cancelled"] is True
    assert observed["finished"] is False


@pytest.mark.unit
async def test_an_invocation_error_propagates_unwrapped() -> None:
    """A graph failure must reach the runner's stage handling as itself — wrapping
    it as a lease problem would mislabel every failure of a leased run."""

    async def _work() -> str:
        raise ValueError("boom")

    renewer = _Renewer(grants=100, interval_seconds=0.01)
    with pytest.raises(ValueError, match="boom"):
        await _with_lease_renewal(_work(), renewer, run_id="r")
