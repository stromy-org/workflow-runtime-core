"""Schema-shape invariants that the retention pass silently depends on.

Retention deletes ``runs`` rows in bulk. Every other table that points at a run
cascades, so the delete carries its dependents away with it — except ``retry_of``,
which points at ``runs`` from ``runs`` and deliberately does not cascade, because a
retry must not be able to erase the attempt it descends from.

That single non-cascading key is why the retention statements carry
``registry._LINEAGE_SAFE``. Without it a failed parent whose retry is still newer
than the retention window aborts the *whole* bulk statement on a foreign-key
violation — so one client's live lineage stops retention for every other client,
and the pass reports success while deleting nothing.

The hazard is not that this key is wrong. It is that the guard is invisible from
the schema: a future migration can add another non-cascading key to ``runs`` in one
line, and nothing about that line looks dangerous. This module makes the FK graph
assert its own shape, so adding one fails here with the reason attached rather than
in production as a retention pass that quietly stops working.

The check runs against a real engine because it is a question about what the
migrations actually built, not about what their source says.
"""

from __future__ import annotations

from workflow_runtime_core import registry
from workflow_runtime_core.migrations import apply_migrations

#: ``(child_table, child_column)`` → why it may point at ``runs`` without
#: cascading, and what keeps retention safe in spite of it.
#:
#: Adding an entry here is a deliberate act with a cost: the retention statements
#: must be taught to exclude rows the new key protects, which is what
#: :data:`registry._LINEAGE_SAFE` does for the one entry below. The companion test
#: refuses an entry the guard never mentions.
_NON_CASCADING_ALLOWED: dict[tuple[str, str], str] = {
    ("runs", "retry_of"): (
        "a retry must not erase its parent's audit history; retention excludes any "
        "run that still has a child via registry._LINEAGE_SAFE"
    ),
}

#: ``confdeltype`` is a single char: 'c' cascade, 'a' no action, 'r' restrict,
#: 'n' set null, 'd' set default.
_CASCADE = "c"

_FOREIGN_KEYS_TO_RUNS = """
SELECT con.conname                  AS name,
       child.relname                AS child_table,
       att.attname                  AS child_column,
       con.confdeltype              AS on_delete,
       array_length(con.conkey, 1)  AS column_count
  FROM pg_constraint con
  JOIN pg_class     child  ON child.oid  = con.conrelid
  JOIN pg_class     parent ON parent.oid = con.confrelid
  JOIN pg_attribute att    ON att.attrelid = con.conrelid
                          AND att.attnum  = con.conkey[1]
 WHERE con.contype = 'f'
   AND parent.relname = 'runs'
"""


def _foreign_keys_to_runs(dsn: str) -> list[dict[str, object]]:
    with registry.connect(dsn) as conn:
        apply_migrations(conn)
        with conn.cursor() as cur:
            cur.execute(_FOREIGN_KEYS_TO_RUNS)
            return list(cur.fetchall())


def test_every_foreign_key_to_runs_cascades_or_is_lineage_guarded(blank_dsn: str) -> None:
    keys = _foreign_keys_to_runs(blank_dsn)

    # A query that matched nothing would make every assertion below vacuous — the
    # exact way a guard passes forever while checking the wrong thing.
    assert keys, "found no foreign keys to runs; the query is wrong, not the schema"

    offenders = [
        f"{k['child_table']}.{k['child_column']} ({k['name']}, on_delete={k['on_delete']!r})"
        for k in keys
        if k["on_delete"] != _CASCADE
        and (k["child_table"], k["child_column"]) not in _NON_CASCADING_ALLOWED
    ]
    assert not offenders, (
        "these foreign keys to runs neither cascade nor are lineage-guarded, so a "
        "bulk retention delete will abort on them and stop retention for every "
        f"client: {offenders}. Either add ON DELETE CASCADE, or extend "
        "registry._LINEAGE_SAFE to exclude the rows they protect and record the "
        "key in _NON_CASCADING_ALLOWED with its reason."
    )


def test_composite_keys_to_runs_are_not_silently_half_read(blank_dsn: str) -> None:
    """The FK query reads ``conkey[1]``, which tells the truth only for single-column
    keys. A composite key would be reported under its first column alone and could
    match an allowlist entry it has nothing to do with, so refuse the shape outright
    rather than let the check above quietly narrow.
    """
    wide = [k["name"] for k in _foreign_keys_to_runs(blank_dsn) if k["column_count"] != 1]
    assert not wide, (
        f"composite foreign keys to runs are not supported by this check: {wide}. "
        "Widen _FOREIGN_KEYS_TO_RUNS to unnest conkey before adding one."
    )


def test_each_allowlisted_key_is_actually_named_by_the_retention_guard() -> None:
    """An allowlist entry claims the retention statements handle that column. If the
    guard never mentions it, the claim is fiction and the first test is waving
    through exactly the key it exists to catch.
    """
    unguarded = [
        f"{table}.{column}"
        for (table, column) in _NON_CASCADING_ALLOWED
        if column not in registry._LINEAGE_SAFE
    ]
    assert not unguarded, (
        f"_NON_CASCADING_ALLOWED exempts {unguarded}, but registry._LINEAGE_SAFE "
        "does not mention those columns, so retention does not actually protect them."
    )
