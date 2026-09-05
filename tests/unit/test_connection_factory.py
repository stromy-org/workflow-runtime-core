"""The connection factory's choices, without touching Azure or a database.

Two things are worth pinning here and neither is obvious from reading the call
sites:

* the sync and async adapters demand DIFFERENT credential types, and the classes
  that satisfy them have IDENTICAL names in ``azure.identity`` and
  ``azure.identity.aio``. Getting it wrong raises ``CredentialValueError`` from
  inside the adapter, at connection time, in whichever process happened to take
  that path first;
* ``AZURE_CLIENT_ID`` selects between a workload's system-assigned and
  user-assigned identity. Two consumers in this estate carry both, and an
  unqualified request resolves to the system-assigned principal — which holds
  nothing. That failure is a 403 long after deployment reported success.
"""

from __future__ import annotations

import pytest

from workflow_runtime_core.auth import (
    AUTH_ENV,
    AZURE_CLIENT_ID_ENV,
    CREDENTIAL_ENV,
    AuthConfigurationError,
    AuthMode,
    CredentialSource,
    build_credential,
    connection_class,
    connection_kwargs,
    principal_from_dsn,
)

pytestmark = pytest.mark.unit

#: The shape Terraform renders for every Entra consumer: principal in the user
#: component, no password anywhere.
_DSN = "postgresql://stromy-workflows-mcp@host:5432/stromy_runtime?sslmode=require"

azure_identity = pytest.importorskip(
    "azure.identity",
    reason="the azure-postgres extra is not installed",
)


# --- the password path is untouched -----------------------------------------


def test_password_mode_uses_plain_psycopg() -> None:
    """No Azure anything on the default path, and no extra kwargs.

    The base install must stay free of Azure dependencies — the public facade
    depends on this package precisely because it does not want them — so the
    password path never reaches the adapter at all.
    """
    import psycopg

    assert connection_class(AuthMode.PASSWORD, is_async=False) is psycopg.Connection
    assert connection_class(AuthMode.PASSWORD, is_async=True) is psycopg.AsyncConnection
    assert connection_kwargs(AuthMode.PASSWORD, _DSN) == {}


# --- the entra path ----------------------------------------------------------


def test_entra_mode_uses_plain_psycopg_classes() -> None:
    """Entra differs from password only in where the password comes from.

    It used to differ in the connection CLASS too, and that adapter class is the
    one that derived a username from token claims — the defect this pins closed.
    """
    import psycopg

    assert connection_class(AuthMode.ENTRA, is_async=False) is psycopg.Connection
    assert connection_class(AuthMode.ENTRA, is_async=True) is psycopg.AsyncConnection


def test_sync_and_async_credentials_come_from_different_modules() -> None:
    """The distinction the adapter enforces, made here instead of at each call.

    A sync caller needs a ``TokenCredential`` and an async one an
    ``AsyncTokenCredential``. Same class names, two modules; a call site that
    imports the wrong one gets a coroutine where it expected a token.
    """
    from azure.core.credentials import TokenCredential
    from azure.core.credentials_async import AsyncTokenCredential

    sync_cred = build_credential(CredentialSource.AZURE_CLI, is_async=False)
    async_cred = build_credential(CredentialSource.AZURE_CLI, is_async=True)

    assert isinstance(sync_cred, TokenCredential)
    assert isinstance(async_cred, AsyncTokenCredential)
    assert type(sync_cred) is not type(async_cred)


def _spy_managed_identity(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, object]]:
    """Record how ManagedIdentityCredential is CONSTRUCTED.

    Asserted at the constructor rather than by inspecting the returned object:
    azure-identity stores the client id inside a nested per-transport credential
    whose shape is a private implementation detail, so a test that reads it back
    would be pinning their internals instead of our decision. What this module
    owns is which arguments it passes.
    """
    import azure.identity

    calls: list[dict[str, object]] = []

    class _Spy:
        def __init__(self, **kwargs: object) -> None:
            calls.append(kwargs)

    monkeypatch.setattr(azure.identity, "ManagedIdentityCredential", _Spy)
    return calls


def test_managed_identity_honours_azure_client_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """A pinned user-assigned identity, not whichever one Azure picks.

    stromy-runner-event and stromy-runtime-prune carry BOTH a system-assigned
    and the shared user-assigned identity. With two attached, an unqualified
    managed-identity token request resolves to the SYSTEM-assigned principal,
    which in this estate is a member of nothing — a 403 on first query, long
    after deployment reported success.
    """
    client_id = "9d19a0fc-56f3-4d65-b54d-046401139c49"
    calls = _spy_managed_identity(monkeypatch)
    monkeypatch.setenv(AZURE_CLIENT_ID_ENV, client_id)

    build_credential(CredentialSource.MANAGED_IDENTITY, is_async=False)
    assert calls == [{"client_id": client_id}]


def test_managed_identity_without_client_id_is_system_assigned(monkeypatch: pytest.MonkeyPatch) -> None:
    """No client_id argument at all — the correct shape for a single identity.

    Passing ``client_id=None`` explicitly is not the same request, which is why
    the factory branches rather than always forwarding the variable.
    """
    calls = _spy_managed_identity(monkeypatch)
    monkeypatch.delenv(AZURE_CLIENT_ID_ENV, raising=False)

    build_credential(CredentialSource.MANAGED_IDENTITY, is_async=False)
    assert calls == [{}]


def test_empty_azure_client_id_is_treated_as_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    """An empty variable is a provisioning gap, not a request for identity ""."""
    calls = _spy_managed_identity(monkeypatch)
    monkeypatch.setenv(AZURE_CLIENT_ID_ENV, "")

    build_credential(CredentialSource.MANAGED_IDENTITY, is_async=False)
    assert calls == [{}]


# --- the principal is read, never derived ------------------------------------
#
# These are the regression tests for the 2026-09-05 outage. The previous
# implementation handed the connection to ``azure-postgresql-auth``, which
# rebuilt the username from the token's ``xms_mirid`` claim and succeeded ONLY
# for a user-assigned identity. Three of five consumers ran on a system-assigned
# one and every database call failed with "Could not retrieve Entra credentials".
#
# The old unit tests passed throughout, because they asserted which CLASS was
# selected and which credential was constructed — never what happened to a token
# of each shape. So the cases below are parametrised over BOTH shapes: the point
# is the population, not the mechanism.

#: A system-assigned identity's claim: the RESOURCE's id. ``parse_principal_name``
#: returns None for this, which is what broke.
_MIRID_SYSTEM = (
    "/subscriptions/000/resourcegroups/rg-x/providers/"
    "Microsoft.App/containerApps/stromy-workflows-mcp"
)
#: A user-assigned identity's claim: the one shape the old path could parse.
_MIRID_USER = (
    "/subscriptions/000/resourcegroups/rg-x/providers/"
    "Microsoft.ManagedIdentity/userAssignedIdentities/stromy-workflow-runner"
)


class _FakeToken:
    def __init__(self, token: str) -> None:
        self.token = token


class _FakeCredential:
    """A credential that yields a token for whichever identity shape is asked for.

    The shape is irrelevant to the code under test now — which is the assertion.
    """

    def __init__(self, mirid: str) -> None:
        self.mirid = mirid
        self.scopes: list[str] = []

    def get_token(self, *scopes: str) -> _FakeToken:
        self.scopes.extend(scopes)
        return _FakeToken(f"token-for::{self.mirid}")


@pytest.mark.parametrize(
    ("shape", "mirid"),
    [("system-assigned", _MIRID_SYSTEM), ("user-assigned", _MIRID_USER)],
)
def test_entra_kwargs_are_identical_for_both_identity_shapes(
    monkeypatch: pytest.MonkeyPatch, shape: str, mirid: str
) -> None:
    """Both shapes produce a password, and neither consults the token's claims.

    Had this existed on 2026-09-04, the system-assigned case would have failed
    and three consumers would never have been deployed broken.
    """
    import workflow_runtime_core.auth as auth_mod

    credential = _FakeCredential(mirid)
    monkeypatch.setattr(auth_mod, "build_credential", lambda *a, **k: credential)
    monkeypatch.setenv(AUTH_ENV, "entra")
    monkeypatch.setenv(CREDENTIAL_ENV, "managed-identity")

    kwargs = connection_kwargs(AuthMode.ENTRA, _DSN)

    assert kwargs == {"password": f"token-for::{mirid}"}, shape
    assert credential.scopes == [auth_mod.PG_TOKEN_SCOPE]
    # The user is NOT injected: it is already in the DSN, and re-deriving it is
    # exactly the behaviour that broke.
    assert "user" not in kwargs


def test_entra_dsn_without_a_principal_is_a_named_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """No password to fall back on and no safe guess — so fail, loudly, here.

    The old path guessed, and its guess returned None for a system-assigned
    identity, so the failure surfaced from inside a token helper as an opaque
    "Could not retrieve Entra credentials" with nothing naming the cause.
    """
    monkeypatch.setenv(AUTH_ENV, "entra")
    monkeypatch.setenv(CREDENTIAL_ENV, "managed-identity")

    with pytest.raises(AuthConfigurationError, match="name the database principal"):
        connection_kwargs(AuthMode.ENTRA, "postgresql://host:5432/stromy_runtime")


@pytest.mark.parametrize(
    ("dsn", "expected"),
    [
        (_DSN, "stromy-workflows-mcp"),
        ("postgresql://stromy-workflow-runner@h:5432/d?sslmode=require", "stromy-workflow-runner"),
        ("host=h dbname=d user=stromy-runner", "stromy-runner"),
    ],
)
def test_principal_is_read_verbatim_from_the_dsn(dsn: str, expected: str) -> None:
    """Keyword and URI forms both resolve, and the value is never transformed.

    Terraform renders this from the same declaration the reconciler grants
    stromy_app to, so any normalisation here would be this module inventing a
    disagreement with the database.
    """
    assert principal_from_dsn(dsn) == expected
