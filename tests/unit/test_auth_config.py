"""The authentication configuration contract.

These are the tests that keep the Entra path OPT-IN. The single most important
property of this release is that a consumer who upgrades and changes nothing
gets the connection behaviour it already had — GMF, the generated workflow
services and the facade all connect with a password DSN as the object owner —
so "unset means password, unset means run" is asserted here rather than assumed.

The rest of the file is about a different kind of safety: role names reach SQL
through ``SET ROLE`` and ``GRANT``, neither of which takes a parameter, so the
validator is the only thing between a config value and an injection.
"""

from __future__ import annotations

import pytest

from workflow_runtime_core.auth import (
    APPLICATION_ROLE_ENV,
    AUTH_ENV,
    CHECKPOINT_SETUP_ENV,
    CREDENTIAL_ENV,
    OWNER_ROLE_ENV,
    AuthConfigurationError,
    AuthMode,
    CheckpointSetupMode,
    CredentialSource,
    resolve_application_role,
    resolve_auth_mode,
    resolve_checkpoint_setup,
    resolve_credential_source,
    resolve_owner_role,
    validate_identifier,
)

pytestmark = pytest.mark.unit


# --- the compatibility floor -------------------------------------------------


def test_auth_mode_defaults_to_password(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unconfigured consumer keeps the behaviour it had before 0.8.0."""
    monkeypatch.delenv(AUTH_ENV, raising=False)
    assert resolve_auth_mode() is AuthMode.PASSWORD


def test_checkpoint_setup_defaults_to_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """Likewise for the checkpointer: setup() still runs unless told otherwise.

    Flipping this default would silently break every existing deployment, whose
    runtime IS the thing that creates the checkpoint store.
    """
    monkeypatch.delenv(CHECKPOINT_SETUP_ENV, raising=False)
    assert resolve_checkpoint_setup() is CheckpointSetupMode.RUN


def test_roles_default_to_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """No role separation unless a deployment asks for it."""
    monkeypatch.delenv(OWNER_ROLE_ENV, raising=False)
    monkeypatch.delenv(APPLICATION_ROLE_ENV, raising=False)
    assert resolve_owner_role() is None
    assert resolve_application_role() is None


# --- precedence --------------------------------------------------------------


def test_explicit_argument_beats_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(AUTH_ENV, "password")
    monkeypatch.setenv(CHECKPOINT_SETUP_ENV, "run")
    monkeypatch.setenv(OWNER_ROLE_ENV, "env_owner")
    assert resolve_auth_mode("entra") is AuthMode.ENTRA
    assert resolve_checkpoint_setup("verify") is CheckpointSetupMode.VERIFY
    assert resolve_owner_role("arg_owner") == "arg_owner"


def test_environment_read_when_no_argument(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(AUTH_ENV, "entra")
    monkeypatch.setenv(CHECKPOINT_SETUP_ENV, "verify")
    monkeypatch.setenv(APPLICATION_ROLE_ENV, "app_role")
    assert resolve_auth_mode() is AuthMode.ENTRA
    assert resolve_checkpoint_setup() is CheckpointSetupMode.VERIFY
    assert resolve_application_role() == "app_role"


@pytest.mark.parametrize("blank", ["", "   ", "\n"])
def test_present_but_empty_is_absent(monkeypatch: pytest.MonkeyPatch, blank: str) -> None:
    """An empty variable is a provisioning gap, not a choice.

    Deployment tooling sets empty strings routinely (an unset Terraform value, a
    templated env with nothing to fill it). Treating "" as a real value turns
    that gap into a confusing validation error far from its cause, so it is read
    as absent and the default applies.
    """
    monkeypatch.setenv(AUTH_ENV, blank)
    monkeypatch.setenv(OWNER_ROLE_ENV, blank)
    assert resolve_auth_mode() is AuthMode.PASSWORD
    assert resolve_owner_role() is None


# --- failing loudly ----------------------------------------------------------


def test_unknown_auth_mode_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(AUTH_ENV, "kerberos")
    with pytest.raises(AuthConfigurationError, match="not a known authentication mode"):
        resolve_auth_mode()


def test_unknown_checkpoint_mode_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(CHECKPOINT_SETUP_ENV, "maybe")
    with pytest.raises(AuthConfigurationError, match="not a known checkpoint setup mode"):
        resolve_checkpoint_setup()


def test_credential_source_has_no_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """The identity a process runs as is never inferred.

    DefaultAzureCredential would succeed with whichever source answers first,
    making the database principal a property of the ambient environment. That is
    the failure this whole plan is about, so an unset value is an error.
    """
    monkeypatch.delenv(CREDENTIAL_ENV, raising=False)
    with pytest.raises(AuthConfigurationError, match="requires WRC_PG_CREDENTIAL"):
        resolve_credential_source()


def test_unknown_credential_source_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(CREDENTIAL_ENV, "environment")
    with pytest.raises(AuthConfigurationError, match="not a known credential source"):
        resolve_credential_source()


def test_known_credential_sources(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(CREDENTIAL_ENV, "managed-identity")
    assert resolve_credential_source() is CredentialSource.MANAGED_IDENTITY
    monkeypatch.setenv(CREDENTIAL_ENV, "azure-cli")
    assert resolve_credential_source() is CredentialSource.AZURE_CLI


# --- identifier validation ---------------------------------------------------


@pytest.mark.parametrize("name", ["stromy_owner", "a", "_x", "R2$d2", "a" * 63])
def test_valid_identifiers_pass(name: str) -> None:
    assert validate_identifier(name, what="role") == name


@pytest.mark.parametrize(
    "name",
    [
        "",
        "1leading_digit",
        "has space",
        "has-hyphen",
        'quote"inside',
        "semi;colon",
        "a" * 64,
        "drop; DROP TABLE runs; --",
        "public.runs",
    ],
)
def test_invalid_identifiers_are_refused_not_quoted(name: str) -> None:
    """Refused, deliberately, rather than escaped.

    SET ROLE and GRANT take identifiers, so the value cannot be bound as a
    parameter. A rejected role name costs an operator five seconds; a quoting
    bug in a privilege statement does not announce itself at all.
    """
    with pytest.raises(AuthConfigurationError, match="plain SQL identifier"):
        validate_identifier(name, what="role")


def test_role_resolution_validates(monkeypatch: pytest.MonkeyPatch) -> None:
    """A bad role reaching SQL from the ENVIRONMENT is the same hazard."""
    monkeypatch.setenv(OWNER_ROLE_ENV, "owner; DROP TABLE runs")
    with pytest.raises(AuthConfigurationError, match="plain SQL identifier"):
        resolve_owner_role()
