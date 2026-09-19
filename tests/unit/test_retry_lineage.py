"""What a retry inherits is a decision per COLUMN, and it must stay exhaustive.

``create_retry`` enumerates the fields it carries forward by hand. That is fine
until someone adds a column: ``create_run`` learns it, this enumeration does
not, and the new field is silently dropped on every retry. Nobody notices,
because a dropped field has no error — it has an absence.

That is exactly how ORG-318 happened. Schema v4 added
``execution_metadata_json``, the facade pinned it at creation, and the retry path
never carried it — so every retried attempt reached the runner looking like a run
that predates the credential plane, bound nothing, and spent whatever ambient
keys the job carried. Measured on attempts 2 and 3 of ``7157f728``: no
``credential_sources`` at any of the four lifecycle points, both retries off the
same parent.

So the classification is data, and this test is the thing that fails when the
next column arrives. It asserts a partition — every field named exactly once —
rather than a subset, because "inherited ∪ per-attempt ∪ minted ⊆ fields" would
pass while a field went unclassified, which is the failure being closed.
"""

from __future__ import annotations

import dataclasses

import pytest

from workflow_runtime_core.models import RunRecord
from workflow_runtime_core.registry import (
    RETRY_INHERITED_FIELDS,
    RETRY_MINTED_FIELDS,
    RETRY_PER_ATTEMPT_FIELDS,
)

#: Fields of ``RunRecord`` that are NOT columns of ``runs`` — they describe how
#: the row was read, not what it holds, so they have no place in a lineage
#: decision. Listed rather than inferred so that adding one is also a deliberate
#: act.
_NOT_A_COLUMN = frozenset({"execution_metadata_column_present"})


def _record_fields() -> frozenset[str]:
    return frozenset(f.name for f in dataclasses.fields(RunRecord)) - _NOT_A_COLUMN


@pytest.mark.unit
def test_every_run_field_has_a_retry_decision() -> None:
    """A new column fails here until someone says what a retry does with it."""
    classified = RETRY_INHERITED_FIELDS | RETRY_PER_ATTEMPT_FIELDS | RETRY_MINTED_FIELDS
    unclassified = _record_fields() - classified
    assert not unclassified, (
        "these RunRecord fields have no retry-lineage decision: "
        f"{sorted(unclassified)}. Add each to RETRY_INHERITED_FIELDS, "
        "RETRY_PER_ATTEMPT_FIELDS or RETRY_MINTED_FIELDS in registry.py — and "
        "make create_retry actually do it."
    )


@pytest.mark.unit
def test_the_classification_names_nothing_that_is_not_a_field() -> None:
    """A renamed column must not leave a stale name looking like coverage."""
    classified = RETRY_INHERITED_FIELDS | RETRY_PER_ATTEMPT_FIELDS | RETRY_MINTED_FIELDS
    assert not classified - _record_fields()


@pytest.mark.unit
@pytest.mark.parametrize(
    ("left", "right"),
    [
        (RETRY_INHERITED_FIELDS, RETRY_PER_ATTEMPT_FIELDS),
        (RETRY_INHERITED_FIELDS, RETRY_MINTED_FIELDS),
        (RETRY_PER_ATTEMPT_FIELDS, RETRY_MINTED_FIELDS),
    ],
)
def test_the_three_sets_are_disjoint(left: frozenset[str], right: frozenset[str]) -> None:
    """A field in two sets is an undecided field wearing a decision."""
    assert not left & right


@pytest.mark.unit
def test_the_execution_snapshot_is_classified_as_inherited() -> None:
    """The regression itself, pinned as a statement rather than a comment.

    Funding is decided once, at creation, against the entitlements of that
    moment. Re-deriving it per attempt — or dropping it, which the runner cannot
    distinguish from re-deriving it to "nothing" — lets an entitlement edited
    between a failure and its retry move who pays for the retry.
    """
    assert "execution_metadata_json" in RETRY_INHERITED_FIELDS


@pytest.mark.unit
def test_a_row_without_the_v4_column_says_so() -> None:
    """The expansion window must stay distinguishable from a dropped snapshot.

    Both read ``execution_metadata_json is None``. Only one of them is a reason
    to fund a run the way runs were funded before the credential plane existed.
    """
    common = {
        "run_id": "00000000-0000-0000-0000-000000000001",
        "workflow": "demo",
        "thread_id": "00000000-0000-0000-0000-000000000001",
        "status": "queued",
        "client_slug": None,
        "config_json": {},
        "image_tag": None,
        "job_template_json": None,
        "created_at": None,
        "updated_at": None,
        "interrupt_payload": None,
        "error": None,
        "artifacts_json": None,
        "idempotency_key": None,
    }
    pre_v4 = RunRecord.from_row(dict(common))
    v4_null = RunRecord.from_row({**common, "execution_metadata_json": None})

    assert pre_v4.execution_metadata_json is None
    assert v4_null.execution_metadata_json is None
    assert pre_v4.execution_metadata_column_present is False
    assert v4_null.execution_metadata_column_present is True
