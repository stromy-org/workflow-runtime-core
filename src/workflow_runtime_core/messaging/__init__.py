"""Durable messaging boundary — ingress, launch, outbox, delivery receipts.

Requires schema v3 (migration ``0003``). This subpackage is part of the BASE
install: it is pure DML plus typed models, so a consumer can run ingress and
egress without acquiring LangGraph. The RabbitMQ *transport* that drives it
lives in :mod:`workflow_runtime_core.transport` behind the ``rabbitmq`` extra.

The four surfaces map one-to-one onto the four crash boundaries:

===================  ====================================================
Boundary             Owner
===================  ====================================================
broker → database    :mod:`.inbox` — ack only after an atomic commit
database → launcher  :mod:`.launches` — leased, retried, reconciled
run → broker         :mod:`.outbox` — written in the terminal transaction
broker → provider    :mod:`.receipts` — definitive vs. ``uncertain``
===================  ====================================================
"""

from __future__ import annotations

from ._backoff import next_delay_seconds
from .envelope import (
    MAX_ATTACHMENTS,
    MAX_ENVELOPE_BYTES,
    SERVICE_NAMESPACE_RE,
    AttachmentRef,
    Envelope,
    EnvelopeError,
    EnvelopeTooLarge,
)
from .inbox import SubmitResult, get_envelope, run_for_source, submit_event
from .launches import Launcher, LaunchRecord, params_hash
from .outbox import OutboxMessage, OutboxRecord, PublishError
from .receipts import DeliveryReceipt

__all__ = [
    "MAX_ATTACHMENTS",
    "MAX_ENVELOPE_BYTES",
    "SERVICE_NAMESPACE_RE",
    "AttachmentRef",
    "DeliveryReceipt",
    "Envelope",
    "EnvelopeError",
    "EnvelopeTooLarge",
    "LaunchRecord",
    "Launcher",
    "OutboxMessage",
    "OutboxRecord",
    "PublishError",
    "SubmitResult",
    "get_envelope",
    "next_delay_seconds",
    "params_hash",
    "run_for_source",
    "submit_event",
]
