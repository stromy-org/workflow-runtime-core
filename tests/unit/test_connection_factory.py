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
    AuthMode,
    CredentialSource,
    build_credential,
    connection_class,
    connection_kwargs,
)

pytestmark = pytest.mark.unit

azure_identity = pytest.importorskip(
    "azure.identity",
    reason="the azure-postgres extra is not installed",
)
pytest.importorskip("azure_postgresql_auth.psycopg3")


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
    assert connection_kwargs(AuthMode.PASSWORD, is_async=False) == {}
    assert connection_kwargs(AuthMode.PASSWORD, is_async=True) == {}


# --- the entra path ----------------------------------------------------------


def test_entra_mode_selects_the_adapter_classes() -> None:
    from azure_postgresql_auth.psycopg3 import AsyncEntraConnection, EntraConnection

    assert connection_class(AuthMode.ENTRA, is_async=False) is EntraConnection
    assert connection_class(AuthMode.ENTRA, is_async=True) is AsyncEntraConnection


def test_sync_and_async_credentials_come_from_different_modules() -> None:
    """The distinction the adapter enforces, made here instead of at each call.

    ``EntraConnection`` requires a sync ``TokenCredential`` and
    ``AsyncEntraConnection`` an ``AsyncTokenCredential``. Same class names, two
    modules; a call site that imports the wrong one fails at connection time
    with a message about credential types rather than about the import.
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


def test_entra_kwargs_carry_a_credential(monkeypatch: pytest.MonkeyPatch) -> None:
    """The adapter REQUIRES one — it constructs none itself.

    A factory that returned no credential would fail every Entra connection with
    "credential is required", which reads as a library bug rather than as
    configuration.
    """
    monkeypatch.setenv(AUTH_ENV, "entra")
    monkeypatch.setenv(CREDENTIAL_ENV, "azure-cli")
    kwargs = connection_kwargs(AuthMode.ENTRA, is_async=False)
    assert "credential" in kwargs
    assert kwargs["credential"] is not None
