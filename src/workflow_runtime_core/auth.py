"""Database authentication: how a connection is opened, and as whom.

Until this module there was exactly one answer — a DSN string carrying a
password — and the deployment that used it gave every workload the local
administrator's credential. This adds a second answer (Microsoft Entra tokens,
acquired per connection from a managed identity) without taking the first away.

Client-neutral by construction
------------------------------
Nothing here knows a role name, a server, or a tenant. Role names arrive as
arguments or environment values and are *validated*, never defaulted: this
package is consumed by an estate whose roles it must not encode, and by
generated services that have none at all. The one thing this module asserts
about a role name is that it is a safe SQL identifier, because that string ends
up in a ``SET ROLE`` that cannot be parameterised.

Everything is opt-in
--------------------
``WRC_PG_AUTH`` defaults to ``password``, so an existing consumer that installs
this version and changes nothing keeps the exact behaviour it had. The Entra
path additionally requires the ``azure-postgres`` extra (``azure-identity``);
without it the failure is a named, actionable error rather than an ImportError
from three frames down.

Why the credential is chosen explicitly
---------------------------------------
``DefaultAzureCredential`` is deliberately NOT used. It tries a chain of sources
and succeeds with whichever answers first, so the identity a process runs as
becomes a property of its ambient environment — the failure this whole plan is
about. ``WRC_PG_CREDENTIAL`` names the source (``managed-identity`` in a hosted
job, ``azure-cli`` at an operator's terminal) and an unset value is an error,
not a guess.

Sync and async credentials are different objects
------------------------------------------------
The two credential classes carry identical names in ``azure.identity`` and
``azure.identity.aio`` while being incompatible objects, so the choice is made
here, once, rather than at each call site.

Why the principal name is READ, never derived
---------------------------------------------
The database user is taken verbatim from the DSN, which Terraform renders from
the same declaration that the reconciler grants ``stromy_app`` to. It is never
inferred from the token.

That is not a stylistic preference; deriving it is a defect this module used to
carry. ``azure-postgresql-auth`` builds the username from the token's
``xms_mirid`` claim, and its ``parse_principal_name`` returns a name ONLY when
that claim ends in ``providers/microsoft.managedidentity/userassignedidentities``
(core.py:91). A SYSTEM-assigned identity's claim is the *resource's* ID, so the
function returns ``None``; a managed-identity token carries no ``upn``,
``preferred_username`` or ``unique_name``; the management-scope retry reads the
same claim shape and also fails — and the whole thing surfaces as the opaque
``Could not retrieve Entra credentials``.

Measured 2026-09-05: that broke three of five consumers (``stromy-workflows-mcp``,
``stromy-runner``, ``stromy-intel-weekly``) while the two on a user-assigned
identity worked, so the estate looked healthy from any single sample. The
derivation adds nothing even when it succeeds — it reconstructs the name the DSN
already carries. So this module acquires the token itself and connects with the
plain psycopg classes. An Entra DSN with no user is a hard, named error, because
the alternative to a declared principal is a guessed one.
"""

from __future__ import annotations

import os
import re
from enum import Enum
from typing import TYPE_CHECKING, Any, cast

from .exceptions import RegistryError

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Sequence

# --- environment contract ----------------------------------------------------

#: ``password`` (default) or ``entra``.
AUTH_ENV = "WRC_PG_AUTH"

#: ``managed-identity`` or ``azure-cli``. No default: see the module docstring.
CREDENTIAL_ENV = "WRC_PG_CREDENTIAL"

#: Role a migration command elevates to before touching anything.
OWNER_ROLE_ENV = "WRC_PG_OWNER_ROLE"

#: Role a migration command reconciles its chain's grants for.
APPLICATION_ROLE_ENV = "WRC_PG_APPLICATION_ROLE"

#: ``run`` (default) or ``verify`` — see :mod:`workflow_runtime_core.executor.checkpointer`.
CHECKPOINT_SETUP_ENV = "WRC_CHECKPOINT_SETUP"

#: Honoured when present so a workload carrying BOTH a system-assigned and a
#: user-assigned identity resolves to the intended one. With two identities
#: attached an unqualified managed-identity token request resolves to the
#: system-assigned principal, which in this estate holds nothing — a 403 long
#: after deployment reported success. The same variable already pins the
#: storage data plane, so honouring it here keeps one answer per process rather
#: than two identities in one container.
AZURE_CLIENT_ID_ENV = "AZURE_CLIENT_ID"


class AuthMode(str, Enum):
    """How a connection authenticates."""

    PASSWORD = "password"  # noqa: S105 - a mode NAME, not a credential
    ENTRA = "entra"


class CredentialSource(str, Enum):
    """Where an Entra token comes from. Never inferred."""

    MANAGED_IDENTITY = "managed-identity"
    AZURE_CLI = "azure-cli"


class CheckpointSetupMode(str, Enum):
    """Whether the checkpoint store may be MIGRATED or only CHECKED."""

    RUN = "run"
    VERIFY = "verify"


class AuthConfigurationError(RegistryError):
    """The authentication configuration is absent, ambiguous or invalid.

    A :class:`~workflow_runtime_core.exceptions.RegistryError` because from a
    caller's point of view it is the same class of failure as an unreachable
    registry: the runtime cannot proceed and must not pretend otherwise.
    """


# --- identifier validation ---------------------------------------------------

# Unquoted-identifier grammar, deliberately narrower than PostgreSQL's own: this
# string is interpolated into `SET ROLE` and `GRANT ... TO`, neither of which
# takes a parameter, so the only safe posture is to accept nothing that would
# need quoting or escaping in the first place. A role name that does not match
# is refused rather than quoted — a rejected name is a five-second fix, and a
# quoting bug in a privilege statement is not.
_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]{0,62}$")


def validate_identifier(value: object, *, what: str) -> str:
    """Return ``value`` if it is a safe SQL identifier, else raise.

    ``what`` names the setting in the error, so an operator who typo'd
    ``--owner-role`` is told which flag to fix rather than being shown a regex.

    The parameter is ``object``, not ``str``: these values arrive from the
    environment and from callers this library does not type-check, so the
    ``isinstance`` guard is a real runtime check rather than a redundant one.
    """
    if not isinstance(value, str) or not _IDENTIFIER_RE.match(value):
        raise AuthConfigurationError(
            f"{what} must be a plain SQL identifier — letters, digits, underscore "
            f"and $ only, starting with a letter or underscore, at most 63 characters. "
            f"Got {value!r}. It is interpolated into SET ROLE / GRANT, which take no "
            f"parameters, so anything needing quoting is refused rather than escaped."
        )
    return value


# --- resolution --------------------------------------------------------------


def _env(name: str) -> str | None:
    raw = os.environ.get(name)
    if raw is None:
        return None
    stripped = raw.strip()
    # A variable that is present but empty is ABSENT, not a value. Deployment
    # tooling sets empty strings routinely, and treating "" as a real choice
    # turns a provisioning gap into a confusing validation error.
    return stripped or None


def resolve_auth_mode(explicit: str | None = None) -> AuthMode:
    """Explicit argument, then ``WRC_PG_AUTH``, then ``password``."""
    raw = explicit or _env(AUTH_ENV)
    if raw is None:
        return AuthMode.PASSWORD
    try:
        return AuthMode(raw)
    except ValueError:
        raise AuthConfigurationError(
            f"{AUTH_ENV}={raw!r} is not a known authentication mode. "
            f"Expected one of: {', '.join(m.value for m in AuthMode)}."
        ) from None


def resolve_credential_source(explicit: str | None = None) -> CredentialSource:
    """Explicit argument, then ``WRC_PG_CREDENTIAL``. **No default.**

    Only reached when the mode is ``entra``. There is deliberately no fallback
    chain: which identity a process authenticates as is the property this whole
    design exists to make explicit, so an unset value fails loudly rather than
    resolving to whatever the environment happens to offer.
    """
    raw = explicit or _env(CREDENTIAL_ENV)
    if raw is None:
        raise AuthConfigurationError(
            f"{AUTH_ENV}=entra requires {CREDENTIAL_ENV} to name the credential source "
            f"({', '.join(c.value for c in CredentialSource)}). It has no default on "
            f"purpose: DefaultAzureCredential would succeed with whichever source "
            f"answers first, making the database identity a property of the ambient "
            f"environment rather than of the deployment."
        )
    try:
        return CredentialSource(raw)
    except ValueError:
        raise AuthConfigurationError(
            f"{CREDENTIAL_ENV}={raw!r} is not a known credential source. "
            f"Expected one of: {', '.join(c.value for c in CredentialSource)}."
        ) from None


def resolve_checkpoint_setup(explicit: str | CheckpointSetupMode | None = None) -> CheckpointSetupMode:
    """Explicit argument, then ``WRC_CHECKPOINT_SETUP``, then ``run``.

    ``run`` stays the default so every existing caller — GMF, generated workflow
    services, anything on a password DSN as the database owner — keeps its
    current behaviour. The hosted estate opts into ``verify``.
    """
    if isinstance(explicit, CheckpointSetupMode):
        return explicit
    raw = explicit or _env(CHECKPOINT_SETUP_ENV)
    if raw is None:
        return CheckpointSetupMode.RUN
    try:
        return CheckpointSetupMode(raw)
    except ValueError:
        raise AuthConfigurationError(
            f"{CHECKPOINT_SETUP_ENV}={raw!r} is not a known checkpoint setup mode. "
            f"Expected one of: {', '.join(m.value for m in CheckpointSetupMode)}."
        ) from None


def resolve_owner_role(explicit: str | None = None) -> str | None:
    """Explicit ``--owner-role``, then ``WRC_PG_OWNER_ROLE``, then ``None``.

    ``None`` means "this deployment does not separate ownership" — the shape
    every consumer had before this version, where the migrating login owns the
    objects itself.
    """
    raw = explicit or _env(OWNER_ROLE_ENV)
    return validate_identifier(raw, what="--owner-role / " + OWNER_ROLE_ENV) if raw else None


def resolve_application_role(explicit: str | None = None) -> str | None:
    """Explicit ``--application-role``, then ``WRC_PG_APPLICATION_ROLE``, else ``None``.

    ``None`` means the chain reconciles no grants, which is correct when the
    application connects as the owner.
    """
    raw = explicit or _env(APPLICATION_ROLE_ENV)
    return validate_identifier(raw, what="--application-role / " + APPLICATION_ROLE_ENV) if raw else None


# --- credentials -------------------------------------------------------------


def _require_azure_extra(exc: ImportError) -> AuthConfigurationError:
    return AuthConfigurationError(
        f"{AUTH_ENV}=entra needs the optional Azure dependencies, which are not "
        f"installed. Install with: uv pip install "
        f"'workflow-runtime-core[azure-postgres]'  (underlying import error: {exc})"
    )


def build_credential(source: CredentialSource, *, is_async: bool) -> Any:
    """Construct the token credential for ``source``.

    ``is_async`` is not a convenience flag. A sync caller needs a
    ``TokenCredential`` and an async one an ``AsyncTokenCredential``: the former's
    ``get_token`` returns a token, the latter's returns a coroutine. The two
    classes carry identical names in ``azure.identity`` and ``azure.identity.aio``,
    so choosing here means a call site cannot get it wrong.
    """
    try:
        if is_async:
            from azure.identity.aio import AzureCliCredential, ManagedIdentityCredential
        else:
            from azure.identity import AzureCliCredential, ManagedIdentityCredential  # type: ignore[assignment]
    except ImportError as exc:  # pragma: no cover - dependency wiring
        raise _require_azure_extra(exc) from exc

    if source is CredentialSource.MANAGED_IDENTITY:
        client_id = _env(AZURE_CLIENT_ID_ENV)
        # Passing client_id pins a specific user-assigned identity; omitting it
        # asks for the system-assigned one. Both are legitimate, and the
        # environment says which — see AZURE_CLIENT_ID_ENV above for why the
        # unqualified request is not a safe default when two are attached.
        return ManagedIdentityCredential(client_id=client_id) if client_id else ManagedIdentityCredential()
    return AzureCliCredential()


#: The audience an Azure Database for PostgreSQL access token must be issued for.
#: Same value ``azure-postgresql-auth`` uses; named here so the connect path owns
#: its own contract rather than importing a constant from a library it no longer
#: connects through.
PG_TOKEN_SCOPE = "https://ossrdbms-aad.database.windows.net/.default"  # noqa: S105 - a public audience URI, not a credential


def principal_from_dsn(dsn: str) -> str:
    """The database role the DSN declares, or a named error.

    Under Entra there is no password to fall back on and no safe way to guess a
    principal, so an absent user is a configuration error surfaced here — at
    connect time, naming the fix — rather than an opaque failure from inside a
    token helper.
    """
    from psycopg.conninfo import conninfo_to_dict  # noqa: PLC0415

    try:
        user = conninfo_to_dict(dsn).get("user")
    except Exception as exc:  # pragma: no cover - malformed DSN
        raise AuthConfigurationError(f"could not parse the connection string: {exc}") from exc
    if not user or not isinstance(user, str):
        raise AuthConfigurationError(
            f"{AUTH_ENV}=entra requires the connection string to name the database "
            "principal (its user component), e.g. "
            "postgresql://<principal>@<host>:5432/<db>?sslmode=require. It is never "
            "derived from the token: a system-assigned identity's token does not "
            "carry a usable name, and guessing one is how a workload silently "
            "authenticates as nobody."
        )
    return user


def _token(credential: Any) -> str:
    return cast("str", credential.get_token(PG_TOKEN_SCOPE).token)


async def _token_async(credential: Any) -> str:
    token = await credential.get_token(PG_TOKEN_SCOPE)
    return cast("str", token.token)


# --- describing the live session ---------------------------------------------

#: Everything `wrc auth-probe` reports. Deliberately all catalog reads: the probe
#: must be safe to run in production, against any role, at any time — including
#: as the application role, which is the case that actually matters.
PROBE_SQL = """
SELECT current_user                                            AS current_user,
       session_user                                            AS session_user,
       current_database()                                      AS database,
       (SELECT array_agg(m.rolname ORDER BY m.rolname)
          FROM pg_auth_members a
          JOIN pg_roles m ON m.oid = a.roleid
         WHERE a.member = (SELECT oid FROM pg_roles WHERE rolname = current_user))
                                                               AS memberships,
       has_database_privilege(current_database(), 'CONNECT')    AS can_connect,
       has_schema_privilege('public', 'USAGE')                  AS schema_usage,
       has_schema_privilege('public', 'CREATE')                 AS schema_create
"""


def describe_session(conn: Any) -> dict[str, Any]:
    """Report who this connection is and what it may do. Never mutates.

    Handles either row factory. Registry connections use ``dict_row``, where a
    naive ``zip(columns, row)`` silently zips the column names against the
    dict's KEYS and produces ``{"current_user": "current_user", ...}`` — a
    report that looks structurally fine and says nothing.
    """
    cur: Any
    with conn.cursor() as cur:
        cur.execute(PROBE_SQL)
        # `cur.description` is Any and `Any or []` widens to `Any | list[Unknown]`,
        # so pin the sequence before iterating it.
        description: Sequence[Any] = cur.description or []
        columns: list[str] = [str(d.name) for d in description]
        row: Any = cur.fetchone()
    if row is None:  # pragma: no cover - would mean the catalog broke
        raise RegistryError("auth probe returned no row")
    if isinstance(row, dict):
        return dict(cast("dict[str, Any]", row))
    return dict(zip(columns, cast("Sequence[Any]", row), strict=True))


def scalar(row: Any) -> Any:
    """First column of a fetched row, whichever row factory produced it.

    Registry connections use ``dict_row`` and ad-hoc ones use the tuple default;
    a helper that assumes either breaks silently on the other — ``row[0]`` on a
    dict raises KeyError, and a privilege check that raises inside a broad
    ``except`` reads as "not permitted".
    """
    if row is None:
        return None
    if isinstance(row, dict):
        return next(iter(cast("dict[str, Any]", row).values()), None)
    return cast("Sequence[Any]", row)[0]


def can_write_table(conn: Any, table: str) -> bool:
    """Does the current principal hold INSERT on ``table``?

    The question a migration command must answer before reporting "already
    current". Read via ``has_table_privilege`` rather than by attempting a write,
    so the probe leaves no row behind and needs no transaction to unwind.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT has_table_privilege(%s, 'INSERT')", (table,))
        return bool(scalar(cur.fetchone()))


def set_role(conn: Any, role: str, *, local: bool = True) -> None:
    """``SET [LOCAL] ROLE <role>``, or raise a named error.

    ``LOCAL`` by default, so the elevation is scoped to the transaction and
    disappears on COMMIT or ROLLBACK — a migrator that crashes mid-run cannot
    leave a session elevated for whatever reuses the connection.

    ``local=False`` is for the one path that has no transaction to scope to:
    ``wrc checkpoint-setup`` must run under autocommit (LangGraph's ``setup()``
    issues ``CREATE INDEX CONCURRENTLY``), and a ``SET LOCAL`` there applies to
    a transaction that ends with the statement itself — elevating for nothing.
    That caller resets the role when it is done.
    """
    from .exceptions import MigrationRoleRequired

    validate_identifier(role, what="owner role")
    scope = "LOCAL " if local else ""
    try:
        with conn.cursor() as cur:
            # Not parameterisable: SET ROLE takes an identifier, not a value.
            # validate_identifier above is what makes this safe.
            cur.execute(f"SET {scope}ROLE {role}")  # noqa: S608 - validated identifier
    except Exception as exc:
        raise MigrationRoleRequired(role, str(exc)) from exc


def connection_kwargs(mode: AuthMode, dsn: str) -> dict[str, Any]:
    """Extra keyword arguments psycopg needs for ``mode`` (synchronous).

    Empty for password auth, so the password path is byte-for-byte what it was.
    Under Entra it is ``{"password": <access token>}`` — the user is already in
    the DSN and is validated here so a missing one fails before the socket opens
    rather than as a server-side authentication error.
    """
    if mode is AuthMode.PASSWORD:
        return {}
    principal_from_dsn(dsn)
    credential = build_credential(resolve_credential_source(), is_async=False)
    return {"password": _token(credential)}


async def connection_kwargs_async(mode: AuthMode, dsn: str) -> dict[str, Any]:
    """Asynchronous twin of :func:`connection_kwargs`.

    Separate rather than an ``is_async`` flag because acquiring the token is an
    await: a flag would force a sync function to return a coroutine on one branch
    and a dict on the other.
    """
    if mode is AuthMode.PASSWORD:
        return {}
    principal_from_dsn(dsn)
    credential = build_credential(resolve_credential_source(), is_async=True)
    try:
        return {"password": await _token_async(credential)}
    finally:
        await credential.close()


def connection_class(mode: AuthMode, *, is_async: bool) -> Any:
    """The psycopg connection class implementing ``mode``.

    Both modes now use the plain psycopg classes. Entra differs only in where the
    password comes from, which is exactly the amount of difference it should ever
    have had — see "Why the principal name is READ, never derived" above.
    """
    import psycopg  # noqa: PLC0415

    return psycopg.AsyncConnection if is_async else psycopg.Connection


__all__ = [
    "APPLICATION_ROLE_ENV",
    "AUTH_ENV",
    "AZURE_CLIENT_ID_ENV",
    "CHECKPOINT_SETUP_ENV",
    "CREDENTIAL_ENV",
    "OWNER_ROLE_ENV",
    "AuthConfigurationError",
    "AuthMode",
    "CheckpointSetupMode",
    "CredentialSource",
    "PG_TOKEN_SCOPE",
    "build_credential",
    "can_write_table",
    "connection_class",
    "connection_kwargs_async",
    "connection_kwargs",
    "describe_session",
    "resolve_application_role",
    "resolve_auth_mode",
    "resolve_checkpoint_setup",
    "resolve_credential_source",
    "resolve_owner_role",
    "set_role",
    "validate_identifier",
]
