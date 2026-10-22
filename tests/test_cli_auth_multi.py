import json
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from mailbox_cleanup.cli import cli
from mailbox_cleanup.config import (
    DEFAULT_CONFIG_PATH_ENV,
    Account,
    Config,
    load_config,
    save_config,
)


@pytest.fixture
def cfg_env(tmp_path, monkeypatch):
    p = tmp_path / "cfg.json"
    monkeypatch.setenv(DEFAULT_CONFIG_PATH_ENV, str(p))
    monkeypatch.delenv("MAILBOX_CLEANUP_ACCOUNT", raising=False)
    return p


def test_auth_set_with_alias_creates_account(cfg_env):
    runner = CliRunner()
    save_config(Config(default=None, accounts=()))
    with patch("mailbox_cleanup.cli.set_credentials") as setcreds:
        r = runner.invoke(
            cli,
            [
                "auth",
                "set",
                "--alias",
                "work",
                "--email",
                "a@b.example",
                "--server",
                "imap.ionos.de",
                "--make-default",
            ],
            input="secret\n",
        )
    assert r.exit_code == 0, r.output
    setcreds.assert_called_once_with("a@b.example", "secret", "imap.ionos.de")
    cfg = load_config()
    assert cfg.default == "work"
    assert cfg.accounts[0].alias == "work"
    assert cfg.accounts[0].email == "a@b.example"
    assert cfg.accounts[0].provider == "ionos"


def test_auth_set_duplicate_alias_fails(cfg_env):
    save_config(
        Config(
            default="work",
            accounts=(Account(alias="work", email="a@b.example", server="imap.ionos.de"),),
        )
    )
    runner = CliRunner()
    with patch("mailbox_cleanup.cli.set_credentials"):
        r = runner.invoke(
            cli,
            ["auth", "set", "--alias", "work", "--email", "z@z.example"],
            input="x\n",
        )
    assert r.exit_code != 0
    assert "duplicate_alias" in r.output


def test_auth_set_duplicate_email_fails(cfg_env):
    save_config(
        Config(
            default="work",
            accounts=(Account(alias="work", email="a@b.example", server="imap.ionos.de"),),
        )
    )
    runner = CliRunner()
    with patch("mailbox_cleanup.cli.set_credentials"):
        r = runner.invoke(
            cli,
            ["auth", "set", "--alias", "office", "--email", "a@b.example"],
            input="x\n",
        )
    assert r.exit_code != 0
    assert "duplicate_email" in r.output


def test_auth_set_no_alias_uses_email_local_part(cfg_env):
    """Without --alias, derive from email and add as account."""
    runner = CliRunner()
    save_config(Config(default=None, accounts=()))
    with patch("mailbox_cleanup.cli.set_credentials"):
        r = runner.invoke(
            cli,
            ["auth", "set", "--email", "owner@example.com"],
            input="pw\n",
        )
    assert r.exit_code == 0, r.output
    cfg = load_config()
    assert cfg.accounts[0].alias == "owner"
    assert cfg.default == "owner"  # auto-default when no other accounts


def test_auth_set_first_account_is_auto_default(cfg_env):
    """When config is empty, the first account becomes default automatically."""
    save_config(Config(default=None, accounts=()))
    runner = CliRunner()
    with patch("mailbox_cleanup.cli.set_credentials"):
        runner.invoke(
            cli,
            ["auth", "set", "--alias", "first", "--email", "f@x.example"],
            input="pw\n",
        )
    cfg = load_config()
    assert cfg.default == "first"


def test_auth_set_subsequent_account_does_not_steal_default(cfg_env):
    save_config(
        Config(
            default="work",
            accounts=(Account(alias="work", email="w@x.example", server="imap.ionos.de"),),
        )
    )
    runner = CliRunner()
    with patch("mailbox_cleanup.cli.set_credentials"):
        runner.invoke(
            cli,
            ["auth", "set", "--alias", "second", "--email", "s@x.example"],
            input="pw\n",
        )
    cfg = load_config()
    assert cfg.default == "work"  # unchanged


def test_auth_test_by_account_alias(cfg_env):
    save_config(
        Config(
            default="work",
            accounts=(Account(alias="work", email="a@b.example", server="imap.ionos.de"),),
        )
    )
    runner = CliRunner()
    fake_mb = MagicMock()
    fake_mb.__enter__.return_value.folder.list.return_value = []
    with (
        patch("mailbox_cleanup.auth.keyring.get_password", return_value="pw"),
        patch("mailbox_cleanup.cli.imap_connect", return_value=fake_mb),
    ):
        r = runner.invoke(cli, ["auth", "test", "--account", "work", "--json"])
    assert r.exit_code == 0, r.output
    data = json.loads(r.output)
    assert data["account"] == "work"


def test_auth_test_email_flag_deprecated_still_works(cfg_env):
    save_config(
        Config(
            default="work",
            accounts=(Account(alias="work", email="a@b.example", server="imap.ionos.de"),),
        )
    )
    runner = CliRunner()
    fake_mb = MagicMock()
    fake_mb.__enter__.return_value.folder.list.return_value = []
    with (
        patch("mailbox_cleanup.auth.keyring.get_password", return_value="pw"),
        patch("mailbox_cleanup.cli.imap_connect", return_value=fake_mb),
    ):
        r = runner.invoke(cli, ["auth", "test", "--email", "a@b.example", "--json"])
    assert r.exit_code == 0, r.output
    data = json.loads(r.output)
    assert data["ok"] is True


def test_auth_delete_by_account_removes_both(cfg_env):
    save_config(
        Config(
            default="work",
            accounts=(
                Account(alias="work", email="a@b.example", server="imap.ionos.de"),
                Account(alias="private", email="c@d.example", server="imap.ionos.de"),
            ),
        )
    )
    runner = CliRunner()
    with (
        patch("mailbox_cleanup.auth.keyring.get_password", return_value="pw"),
        patch("mailbox_cleanup.auth.keyring.delete_password") as kdel,
    ):
        r = runner.invoke(cli, ["auth", "delete", "--account", "work"])
    assert r.exit_code == 0, r.output
    cfg = load_config()
    assert [a.alias for a in cfg.accounts] == ["private"]
    assert cfg.default is None
    kdel.assert_called()


def test_auth_delete_unknown_fails(cfg_env):
    save_config(
        Config(
            default="work",
            accounts=(Account(alias="work", email="a@b.example", server="imap.ionos.de"),),
        )
    )
    runner = CliRunner()
    with patch("mailbox_cleanup.auth.keyring.get_password", return_value="pw"):
        r = runner.invoke(cli, ["auth", "delete", "--account", "ghost"])
    assert r.exit_code != 0
    assert "unknown_account" in r.output
