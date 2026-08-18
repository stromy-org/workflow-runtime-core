"""The client-facing half of execution metadata: what escapes, and what cannot.

These are unit tests because the property under test is a *projection*, not a
query. Asserting it against a database would prove the same thing more slowly
while making it easy to believe the allowlist is enforced by SQL — it is not.
The stored column is server-derived and operator-facing; :func:`public` is the
only thing between it and a client.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from workflow_runtime_core.models import (
    PUBLIC_EXECUTION_METADATA_KEYS,
    RunRecord,
    RunStatus,
    public_execution_metadata,
)
from workflow_runtime_core.registry import CORE_EVENT_KINDS, RegistryError, record_event

_PINNED = {
    "credential_policy": "client",
    "credential_subject": {"kind": "client", "id": "acme-co"},
    "credentials": ["deepseek-api", "openai-api"],
    "model_registry_digest": "sha256:beef",
}


def _run(**overrides: object) -> RunRecord:
    now = datetime(2026, 8, 18, tzinfo=UTC)
    base: dict[str, object] = {
        "run_id": "r-1",
        "workflow": "demo",
        "thread_id": "r-1",
        "status": RunStatus.COMPLETED,
        "client_slug": "acme-co",
        "config_json": {},
        "image_tag": None,
        "job_template_json": None,
        "created_at": now,
        "updated_at": now,
        "interrupt_payload": None,
        "error": None,
        "artifacts_json": None,
        "idempotency_key": None,
    }
    base.update(overrides)
    return RunRecord(**base)  # type: ignore[arg-type]


# --- the allowlist ------------------------------------------------------------


@pytest.mark.unit
def test_projection_keeps_only_credential_sources() -> None:
    stored = {"pinned": _PINNED, "credential_sources": {"1": {"openai-api": "client-registered"}}}
    assert public_execution_metadata(stored) == {
        "credential_sources": {"1": {"openai-api": "client-registered"}}
    }


@pytest.mark.unit
def test_projection_never_leaks_the_pinned_block() -> None:
    """The pinned block names the policy, the subject and the registry digest.

    None of it is secret, and all of it is a statement about how the platform is
    configured rather than about this run's outcome. Asserted as a whole-value
    equality above and again by absence here, because the failure mode is a
    future key being ADDED to the pin and silently riding out with it.
    """
    projected = public_execution_metadata({"pinned": _PINNED})
    assert projected == {}
    assert "credential_policy" not in projected
    assert "model_registry_digest" not in projected


@pytest.mark.unit
def test_projection_ignores_unknown_top_level_keys() -> None:
    """A key this build has never heard of is dropped, not passed through.

    This is the allowlist's whole reason for existing: an older reader running
    against a newer writer must not forward a field whose sensitivity it cannot
    know. A denylist would forward it.
    """
    assert public_execution_metadata({"some_future_field": {"secret": "value"}}) == {}


@pytest.mark.unit
@pytest.mark.parametrize("raw", [None, "not-a-dict", 42, []])
def test_projection_of_a_non_object_is_empty(raw: object) -> None:
    assert public_execution_metadata(raw) == {}  # type: ignore[arg-type]


@pytest.mark.unit
def test_the_allowlist_is_exactly_one_key() -> None:
    """Pinned deliberately. Widening it is a decision, not a refactor."""
    assert PUBLIC_EXECUTION_METADATA_KEYS == frozenset({"credential_sources"})


# --- the public run projection ------------------------------------------------


@pytest.mark.unit
def test_public_omits_execution_when_the_column_is_absent() -> None:
    """A v1/v2/v3 registry cannot answer the question at all.

    Absent, not ``"execution": null`` — the same distinction the v2 keys make.
    A null reads as "nothing funded this run", which is false and worse than
    silence.
    """
    assert "execution" not in _run().public()


@pytest.mark.unit
def test_public_omits_execution_when_only_the_pin_is_stored() -> None:
    """Pinned but never observed: the run has not resolved credentials yet.

    Nothing client-facing exists to report, and the pin itself is not it.
    """
    assert "execution" not in _run(execution_metadata_json={"pinned": _PINNED}).public()


@pytest.mark.unit
def test_public_surfaces_credential_sources_and_nothing_else() -> None:
    run = _run(
        execution_metadata_json={
            "pinned": _PINNED,
            "credential_sources": {"1": {"openai-api": "client-registered"}},
        }
    )
    payload = run.public()
    assert payload["execution"] == {
        "credential_sources": {"1": {"openai-api": "client-registered"}}
    }
    assert "acme-co" not in str(payload["execution"])
    assert "sha256:beef" not in str(payload)


# --- record_event -------------------------------------------------------------
#
# The rejections need no database: validation runs before the INSERT, so the
# connection is never touched. Passing a sentinel proves that too — a check that
# reached the database would fail on the sentinel instead of raising.


@pytest.mark.unit
@pytest.mark.parametrize("kind", ["Credentials Resolved", "UPPER", "has-hyphen", "", "9leading"])
def test_record_event_refuses_an_unqueryable_kind(kind: str) -> None:
    with pytest.raises(RegistryError, match="invalid event kind"):
        record_event(object(), "r-1", kind)  # type: ignore[arg-type]


@pytest.mark.unit
@pytest.mark.parametrize("kind", sorted(CORE_EVENT_KINDS))
def test_record_event_refuses_every_lifecycle_kind(kind: str) -> None:
    """Enumerated from the set itself, so a kind added to the lifecycle is
    covered the moment it is added rather than when someone remembers to."""
    with pytest.raises(RegistryError, match="written by the run lifecycle"):
        record_event(object(), "r-1", kind)  # type: ignore[arg-type]


@pytest.mark.unit
def test_record_event_accepts_a_consumer_kind() -> None:
    """The positive case cannot avoid the database, so it only gets as far as
    proving the kind passed validation — the sentinel then fails on the INSERT."""
    with pytest.raises(AttributeError):
        record_event(object(), "r-1", "credentials_resolved", {"count": 2})  # type: ignore[arg-type]
