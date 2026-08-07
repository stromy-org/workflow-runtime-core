"""The normalized inbound event — the one shape every channel adapter produces.

A channel adapter (a WhatsApp relay, an email poller, a webhook receiver)
translates its own wire format into an :class:`Envelope` and hands it to
:func:`~workflow_runtime_core.messaging.inbox.submit_event`. Nothing downstream
of that boundary knows which channel a message came from, which is what lets one
runner serve every lane.

Two limits are enforced here rather than trusted:

* **256 KiB encoded.** The envelope is persisted verbatim in a JSONB column and
  re-read on every recovery path. An unbounded envelope turns one oversized
  inbound message into a permanently unreadable row that fails the same way on
  every retry — a poison pill that looks like a database problem.
* **20 attachment descriptors, and descriptors only.** Bytes never enter the
  envelope. A base64 image in a JSONB column is paid for on every read, every
  backup and every replication hop, and it puts client content in a place the
  redaction rules cannot reach. An attachment is therefore a media type, a size,
  a digest and an expiring reference to an object store.

Both raise :class:`EnvelopeTooLarge` at *submit* time — before the row exists —
because the alternative is discovering it at recovery time, when the only
options left are bad ones.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from ..exceptions import WorkflowRuntimeCoreError

#: Maximum size of the UTF-8 encoded envelope JSON.
MAX_ENVELOPE_BYTES = 256 * 1024

#: Maximum number of attachment descriptors on one envelope.
MAX_ATTACHMENTS = 20

#: ``service_namespace`` is used to derive broker entity names (exchanges,
#: queues, DLQs), so it must be DNS-safe. It is also part of every uniqueness
#: boundary in schema v3, which is why it is immutable after the first persisted
#: run: changing it would silently reset every idempotency key at once.
SERVICE_NAMESPACE_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _empty_payload() -> dict[str, Any]:
    """Typed factory for the default payload.

    ``default_factory=dict`` reads better but leaves the element types unknown
    under pyright strict, which then propagates ``Unknown`` into every caller
    that touches ``payload``.
    """
    return {}


class EnvelopeError(WorkflowRuntimeCoreError, ValueError):
    """An envelope violated the contract and was never persisted."""


class EnvelopeTooLarge(EnvelopeError):
    """The envelope exceeded a bound. Raised before any row is written."""


@dataclass(frozen=True)
class AttachmentRef:
    """A pointer to bytes that live in an object store, never the bytes.

    ``digest`` is the content address the receiving side verifies against, so a
    reference that silently starts pointing at different content is detectable.
    ``reference`` is expected to be short-lived (a pre-signed URL); it is
    redacted from logs and metrics because it is a bearer credential.
    """

    media_type: str
    size_bytes: int
    digest: str
    reference: str
    filename: str | None = None

    def validate(self) -> None:
        if not self.media_type.strip():
            raise EnvelopeError("attachment media_type must not be empty")
        if self.size_bytes < 0:
            raise EnvelopeError(f"attachment size_bytes must be >= 0, got {self.size_bytes}")
        if not _SHA256_RE.match(self.digest):
            raise EnvelopeError(
                f"attachment digest must be a lowercase hex sha256, got {self.digest!r}"
            )
        if not self.reference.strip():
            raise EnvelopeError("attachment reference must not be empty")

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "media_type": self.media_type,
            "size_bytes": self.size_bytes,
            "digest": self.digest,
            "reference": self.reference,
        }
        if self.filename is not None:
            payload["filename"] = self.filename
        return payload

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> AttachmentRef:
        return cls(
            media_type=raw["media_type"],
            size_bytes=int(raw["size_bytes"]),
            digest=raw["digest"],
            reference=raw["reference"],
            filename=raw.get("filename"),
        )


@dataclass(frozen=True)
class Envelope:
    """One normalized inbound event.

    ``source`` + ``source_message_id`` is the channel's own identity for this
    message and forms the idempotency key together with ``service_namespace``.
    It must be the *channel's* id (a Twilio SID, a Message-ID header, a webhook
    delivery id) and never one this process generates: a freshly generated id is
    different on every redelivery, which is precisely when de-duplication has to
    work.
    """

    service_namespace: str
    source: str
    source_message_id: str
    workflow: str
    payload: dict[str, Any] = field(default_factory=_empty_payload)
    attachments: tuple[AttachmentRef, ...] = ()
    reply_to: dict[str, Any] | None = None
    correlation_id: str | None = None
    #: Bumped when the persisted shape changes incompatibly. Recovery reads rows
    #: written by older builds, so the version travels with the row.
    schema_version: int = 1

    def validate(self) -> None:
        """Enforce every bound. Raises rather than truncating.

        Truncation would be worse than refusal: a silently shortened envelope
        produces a run whose input differs from what the channel actually sent,
        and nothing downstream can tell.
        """
        if not SERVICE_NAMESPACE_RE.match(self.service_namespace):
            raise EnvelopeError(
                f"service_namespace {self.service_namespace!r} is not DNS-safe; it must "
                f"match {SERVICE_NAMESPACE_RE.pattern} (it names broker entities and is "
                "part of every uniqueness boundary)"
            )
        for name in ("source", "source_message_id", "workflow"):
            if not str(getattr(self, name)).strip():
                raise EnvelopeError(f"{name} must not be empty")

        if len(self.attachments) > MAX_ATTACHMENTS:
            raise EnvelopeTooLarge(
                f"{len(self.attachments)} attachment descriptors exceeds the limit of "
                f"{MAX_ATTACHMENTS}. Attachments are references, so a message needing more "
                "than this is a bulk transfer wearing a message's clothes — move it to the "
                "object store and send one manifest reference."
            )
        for attachment in self.attachments:
            attachment.validate()

        encoded = self.encoded()
        if len(encoded) > MAX_ENVELOPE_BYTES:
            raise EnvelopeTooLarge(
                f"envelope is {len(encoded)} bytes encoded, over the {MAX_ENVELOPE_BYTES} "
                "byte limit. Envelope JSON is persisted and re-read on every recovery path; "
                "put large content in the object store and reference it."
            )

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "service_namespace": self.service_namespace,
            "source": self.source,
            "source_message_id": self.source_message_id,
            "workflow": self.workflow,
            "payload": self.payload,
            "attachments": [a.as_dict() for a in self.attachments],
            "reply_to": self.reply_to,
            "correlation_id": self.correlation_id,
        }

    def encoded(self) -> bytes:
        """The exact bytes the size limit is measured against.

        ``sort_keys`` so the measurement is stable regardless of dict insertion
        order — otherwise the same envelope could pass or fail depending on how
        its adapter happened to build the payload.
        """
        return json.dumps(self.as_dict(), sort_keys=True, default=str).encode("utf-8")

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Envelope:
        raw_attachments: list[dict[str, Any]] = raw.get("attachments") or []
        raw_payload: dict[str, Any] = raw.get("payload") or {}
        return cls(
            service_namespace=raw["service_namespace"],
            source=raw["source"],
            source_message_id=raw["source_message_id"],
            workflow=raw["workflow"],
            payload=raw_payload,
            attachments=tuple(AttachmentRef.from_dict(a) for a in raw_attachments),
            reply_to=raw.get("reply_to"),
            correlation_id=raw.get("correlation_id"),
            schema_version=int(raw.get("schema_version", 1)),
        )
