"""Broker transports driving the durable messaging boundary.

Kept apart from :mod:`workflow_runtime_core.messaging` so the base install stays
free of ``aio-pika``: the workflow facade imports the messaging models and never
acquires a broker client. Importing this module without the ``rabbitmq`` extra
raises a :class:`~workflow_runtime_core.exceptions.DependencyError` naming the
extra to install.

The transport is per-lane by the 2026-08-06 fork resolution — RabbitMQ for a
client-owned service, Azure Storage Queue for the hosted plane — while both
drive the same core lease/claim primitives. Adding a transport means adding a
module here, never a schema change.
"""

from __future__ import annotations

from .topology import BrokerTopology

__all__ = ["BrokerTopology"]
