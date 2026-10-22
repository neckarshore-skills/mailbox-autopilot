import json
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from mailbox_cleanup.cli import cli
from mailbox_cleanup.config import (
    DEFAULT_CONFIG_PATH_ENV,
    Account,
    Config,
    save_config,
)


@pytest.fixture
def cfg_env(tmp_path, monkeypatch):
    p = tmp_path / "cfg.json"
    monkeypatch.setenv(DEFAULT_CONFIG_PATH_ENV, str(p))
    monkeypatch.delenv("MAILBOX_CLEANUP_ACCOUNT", raising=False)
    return p


def test_cli_help():
    runner = CliRunner()
    result = runner.invoke(cli, ["--help"])
    assert result.exit_code == 0
    assert "auth" in result.output


def test_auth_set_writes_to_keychain_and_config(cfg_env):
    runner = CliRunner()
    with patch("mailbox_cleanup.cli.set_credentials") as set_creds:
        result = runner.invoke(
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
            ],
            input="my-password\n",
        )
        assert result.exit_code == 0, result.output
        set_creds.assert_called_once_with("a@b.example", "my-password", "imap.ionos.de")


def test_auth_test_success_json(cfg_env):
    save_config(
        Config(
            default="work",
            accounts=(Account(alias="work", email="a@b.example", server="imap.ionos.de"),),
        )
    )
    runner = CliRunner()
    fake_client = MagicMock()
    inbox = MagicMock()
    inbox.name = "INBOX"
    sent = MagicMock()
    sent.name = "Sent"
    fake_client.__enter__.return_value.folder.list.return_value = [inbox, sent]
    with (
        patch("mailbox_cleanup.auth.keyring.get_password", return_value="pw"),
        patch("mailbox_cleanup.cli.imap_connect", return_value=fake_client),
    ):
        result = runner.invoke(cli, ["auth", "test", "--account", "work", "--json"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["ok"] is True
        assert data["email"] == "a@b.example"
        assert data["account"] == "work"
        assert "folders" in data


def test_auth_test_missing_credentials_exit_3(cfg_env):
    save_config(
        Config(
            default="work",
            accounts=(Account(alias="work", email="a@b.example", server="imap.ionos.de"),),
        )
    )
    runner = CliRunner()
    with patch("mailbox_cleanup.auth.keyring.get_password", return_value=None):
        result = runner.invoke(cli, ["auth", "test", "--account", "work", "--json"])
        assert result.exit_code == 3, result.output
        data = json.loads(result.output)
        assert data["ok"] is False
        assert data["error_code"] == "auth_missing"
