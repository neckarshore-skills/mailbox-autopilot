from unittest.mock import patch

import pytest

from mailbox_cleanup.cli_helpers import (
    AccountFlagsError,
    resolve_account_and_credentials,
)
from mailbox_cleanup.config import (
    DEFAULT_CONFIG_PATH_ENV,
    Account,
    Config,
    save_config,
)


def _make_existing_config(tmp_path, monkeypatch, *, default="work"):
    p = tmp_path / "cfg.json"
    monkeypatch.setenv(DEFAULT_CONFIG_PATH_ENV, str(p))
    save_config(
        Config(
            default=default,
            accounts=(
                Account(alias="work", email="work@x.example", server="imap.ionos.de"),
                Account(alias="private", email="priv@y.example", server="imap.ionos.de"),
            ),
        )
    )
    return p


def _patch_auth_keyring(passwords: dict):
    """Mock auth.get_credentials's keyring lookup."""
    return patch(
        "mailbox_cleanup.auth.keyring.get_password",
        side_effect=lambda service, account: passwords.get(account),
    )


def test_resolves_via_account_flag(tmp_path, monkeypatch):
    _make_existing_config(tmp_path, monkeypatch)
    monkeypatch.delenv("MAILBOX_CLEANUP_ACCOUNT", raising=False)
    with _patch_auth_keyring({"work@x.example": "pw1", "priv@y.example": "pw2"}):
        account, creds = resolve_account_and_credentials(account_flag="private", email_flag=None)
    assert account.alias == "private"
    assert creds.email == "priv@y.example"
    assert creds.password == "pw2"


def test_email_flag_used_as_account_with_deprecation_warning(tmp_path, monkeypatch):
    _make_existing_config(tmp_path, monkeypatch)
    monkeypatch.delenv("MAILBOX_CLEANUP_ACCOUNT", raising=False)
    with _patch_auth_keyring({"work@x.example": "pw1", "priv@y.example": "pw2"}):
        with pytest.warns(DeprecationWarning, match="--email is deprecated"):
            account, _ = resolve_account_and_credentials(
                account_flag=None, email_flag="work@x.example"
            )
    assert account.alias == "work"


def test_account_flag_overrides_email_flag(tmp_path, monkeypatch):
    _make_existing_config(tmp_path, monkeypatch)
    monkeypatch.delenv("MAILBOX_CLEANUP_ACCOUNT", raising=False)
    with _patch_auth_keyring({"work@x.example": "p", "priv@y.example": "p"}):
        account, _ = resolve_account_and_credentials(
            account_flag="private", email_flag="work@x.example"
        )
    assert account.alias == "private"


def test_env_var_used_when_no_flags(tmp_path, monkeypatch):
    _make_existing_config(tmp_path, monkeypatch)
    monkeypatch.setenv("MAILBOX_CLEANUP_ACCOUNT", "private")
    with _patch_auth_keyring({"work@x.example": "p", "priv@y.example": "p"}):
        account, _ = resolve_account_and_credentials(account_flag=None, email_flag=None)
    assert account.alias == "private"


def test_falls_back_to_default(tmp_path, monkeypatch):
    _make_existing_config(tmp_path, monkeypatch)
    monkeypatch.delenv("MAILBOX_CLEANUP_ACCOUNT", raising=False)
    with _patch_auth_keyring({"work@x.example": "p", "priv@y.example": "p"}):
        account, _ = resolve_account_and_credentials(account_flag=None, email_flag=None)
    assert account.alias == "work"  # default


def test_auto_bootstrap_when_no_config_and_email_flag(tmp_path, monkeypatch, capsys):
    p = tmp_path / "cfg.json"
    monkeypatch.setenv(DEFAULT_CONFIG_PATH_ENV, str(p))
    monkeypatch.delenv("MAILBOX_CLEANUP_ACCOUNT", raising=False)
    assert not p.exists()

    keychain = {
        ("mailbox-cleanup", "owner@example.com"): "pw",
        ("mailbox-cleanup", "imap-server:owner@example.com"): "imap.ionos.de",
    }

    def cfg_get(service, key):
        return keychain.get((service, key))

    def auth_get(service, key):
        return keychain.get((service, key))

    with (
        patch("mailbox_cleanup.config.keyring") as cfg_kr,
        patch(
            "mailbox_cleanup.auth.keyring.get_password",
            side_effect=auth_get,
        ),
    ):
        cfg_kr.get_password.side_effect = cfg_get
        cfg_kr.delete_password.return_value = None
        with pytest.warns(DeprecationWarning):
            account, creds = resolve_account_and_credentials(
                account_flag=None, email_flag="owner@example.com"
            )

    assert p.exists()
    assert account.alias == "owner"
    assert creds.password == "pw"
    err = capsys.readouterr().err
    assert "Migrated to multi-account config" in err


def test_no_config_no_flags_raises(tmp_path, monkeypatch):
    p = tmp_path / "cfg.json"
    monkeypatch.setenv(DEFAULT_CONFIG_PATH_ENV, str(p))
    monkeypatch.delenv("MAILBOX_CLEANUP_ACCOUNT", raising=False)
    with pytest.raises(AccountFlagsError, match="no_config"):
        resolve_account_and_credentials(account_flag=None, email_flag=None)


def test_unknown_account_raises_account_flags_error(tmp_path, monkeypatch):
    _make_existing_config(tmp_path, monkeypatch)
    monkeypatch.delenv("MAILBOX_CLEANUP_ACCOUNT", raising=False)
    with pytest.raises(AccountFlagsError) as exc:
        resolve_account_and_credentials(account_flag="ghost", email_flag=None)
    assert exc.value.error_code == "unknown_account"


def test_account_flags_error_carries_error_code():
    err = AccountFlagsError("unknown_account", "test message")
    assert err.error_code == "unknown_account"
    assert "test message" in str(err)


def test_email_deprecation_names_no_version():
    """Founder decision 2026-10-08: the flag stays, and the message stops promising a
    version. It said "Removed in v0.3", then "v0.4", and each release made it false."""
    import inspect

    from mailbox_cleanup import cli_helpers

    src = inspect.getsource(cli_helpers)
    assert "--email is deprecated" in src
    assert "Removed in v" not in src
