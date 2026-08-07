"""Broker entity names, derived from the service namespace.

Every exchange, queue and DLQ name is computed from ``SERVICE_NAMESPACE``.
Nothing is called ``inbound`` or ``outbound``, and that is a hard rule rather
than a style preference: two services sharing a broker with generic names bind
to each other's queues, and the failure is silent — messages simply arrive
somewhere unexpected and are acknowledged by a consumer that had no business
seeing them. Deriving names from a namespace that is validated DNS-safe and
immutable after the first persisted run makes the collision impossible instead
of unlikely.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..messaging.envelope import SERVICE_NAMESPACE_RE, EnvelopeError


@dataclass(frozen=True)
class BrokerTopology:
    """The complete set of broker entities one service owns."""

    service_namespace: str

    def __post_init__(self) -> None:
        if not SERVICE_NAMESPACE_RE.match(self.service_namespace):
            raise EnvelopeError(
                f"service_namespace {self.service_namespace!r} is not DNS-safe; broker "
                f"entity names are derived from it (must match {SERVICE_NAMESPACE_RE.pattern})"
            )

    @property
    def inbound_exchange(self) -> str:
        return f"{self.service_namespace}.inbound"

    @property
    def inbound_queue(self) -> str:
        return f"{self.service_namespace}.inbound.q"

    @property
    def dead_letter_exchange(self) -> str:
        return f"{self.service_namespace}.dlx"

    @property
    def dead_letter_queue(self) -> str:
        """Where the broker parks a message that exceeded its delivery limit.

        Distinct from the error queue below: this one holds messages that were
        *valid enough to try* and kept failing, so it is a retry-exhaustion
        signal and its depth is an alert.
        """
        return f"{self.service_namespace}.dlq"

    @property
    def error_queue(self) -> str:
        """Where a message we could not even parse is parked, with a reason.

        Separate from the DLQ on purpose. A malformed payload will never
        succeed no matter how many times it is redelivered, so retrying it is
        pure waste; it is republished here once, with a machine-readable code,
        and then the original is acknowledged.
        """
        return f"{self.service_namespace}.error.q"

    @property
    def outbound_exchange(self) -> str:
        return f"{self.service_namespace}.outbound"

    def destination_queue(self, destination: str) -> str:
        """Queue backing one outbound destination (``whatsapp.reply``, ...)."""
        return f"{self.service_namespace}.out.{destination}"
