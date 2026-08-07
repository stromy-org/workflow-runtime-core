"""Capped exponential backoff with jitter, shared by every retrying loop.

Database-backed retry, deliberately — not broker requeue. A ``nack(requeue=True)``
loop re-delivers immediately and spins the broker at full speed against a
dependency that is already struggling; the row-based schedule below lets a
failing destination back off without the broker noticing at all.

Jitter is not decoration. Without it, N replicas that failed against the same
downstream at the same moment retry at the same moment forever, so the
thundering herd that caused the outage reassembles itself on every cycle. Full
jitter (uniform over ``[0, capped]``) spreads them, at the cost of some retries
being sooner than the nominal schedule — which is the trade we want, because the
schedule's job is to bound load, not to be precise.
"""

from __future__ import annotations

import random

#: Delay before the first retry.
BASE_DELAY_SECONDS = 2.0

#: Ceiling on the exponential term. ~17 minutes: long enough that a sustained
#: outage costs almost nothing, short enough that recovery is not gated on an
#: operator noticing.
MAX_DELAY_SECONDS = 1024.0


def next_delay_seconds(
    attempts: int,
    *,
    base: float = BASE_DELAY_SECONDS,
    cap: float = MAX_DELAY_SECONDS,
    rng: random.Random | None = None,
) -> float:
    """Seconds to wait before attempt number ``attempts + 1``.

    ``attempts`` is the number of attempts ALREADY made, so the first failure
    (``attempts == 1``) yields a delay around ``base``.

    Returns a value in ``[0, min(cap, base * 2 ** (attempts - 1))]``. The lower
    bound really is zero: full jitter is what decorrelates replicas, and a
    retry that happens immediately is harmless when the cohort as a whole is
    spread out.
    """
    if attempts <= 0:
        return 0.0
    # 2 ** (attempts - 1) overflows into absurd floats long before it matters,
    # so clamp the EXPONENT rather than the result.
    exponent = min(attempts - 1, 32)
    ceiling = min(cap, base * (2.0**exponent))
    return (rng or random).uniform(0.0, ceiling)
