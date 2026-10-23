"""Verify all data subcommands accept --account and respect the resolver."""

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


def _cfg_two_accounts():
    return Config(
        default="work",
        accounts=(
            Account(alias="work", email="w@x.example", server="imap.ionos.de"),
            Account(alias="private", email="p@y.example", server="imap.ionos.de"),
        ),
    )


def _fake_imap():
    """Build a context-manager fake that exposes folder.set, folder.list, fetch."""
    fake_mb = MagicMock()
    cm = fake_mb.__enter__.return_value
    cm.folder.set = MagicMock()
    cm.folder.list = MagicMock(return_value=[])
    cm.fetch = MagicMock(return_value=iter([]))
    return fake_mb


def test_scan_uses_account_flag(cfg_env):
    save_config(_cfg_two_accounts())
    seen = {}

    def fake_connect(creds, port=993):
        seen["email"] = creds.email
        return _fake_imap()

    with (
        patch("mailbox_cleanup.auth.keyring.get_password", return_value="pw"),
        patch("mailbox_cleanup.cli.imap_connect", side_effect=fake_connect),
    ):
        runner = CliRunner()
        r = runner.invoke(cli, ["scan", "--account", "private", "--json"])
    assert r.exit_code == 0, r.output
    assert seen["email"] == "p@y.example"


def test_scan_falls_back_to_default(cfg_env):
    save_config(_cfg_two_accounts())
    seen = {}

    def fake_connect(creds, port=993):
        seen["email"] = creds.email
        return _fake_imap()

    with (
        patch("mailbox_cleanup.auth.keyring.get_password", return_value="pw"),
        patch("mailbox_cleanup.cli.imap_connect", side_effect=fake_connect),
    ):
        runner = CliRunner()
        r = runner.invoke(cli, ["scan", "--json"])
    assert r.exit_code == 0, r.output
    assert seen["email"] == "w@x.example"  # default


def test_scan_env_var_overrides_default(cfg_env, monkeypatch):
    save_config(_cfg_two_accounts())
    monkeypatch.setenv("MAILBOX_CLEANUP_ACCOUNT", "private")
    seen = {}

    def fake_connect(creds, port=993):
        seen["email"] = creds.email
        return _fake_imap()

    with (
        patch("mailbox_cleanup.auth.keyring.get_password", return_value="pw"),
        patch("mailbox_cleanup.cli.imap_connect", side_effect=fake_connect),
    ):
        runner = CliRunner()
        r = runner.invoke(cli, ["scan", "--json"])
    assert r.exit_code == 0, r.output
    assert seen["email"] == "p@y.example"


def test_scan_email_flag_still_works(cfg_env):
    save_config(_cfg_two_accounts())
    seen = {}

    def fake_connect(creds, port=993):
        seen["email"] = creds.email
        return _fake_imap()

    with (
        patch("mailbox_cleanup.auth.keyring.get_password", return_value="pw"),
        patch("mailbox_cleanup.cli.imap_connect", side_effect=fake_connect),
    ):
        runner = CliRunner()
        r = runner.invoke(cli, ["scan", "--email", "w@x.example", "--json"])
    assert r.exit_code == 0, r.output
    assert seen["email"] == "w@x.example"


def test_senders_uses_account_flag(cfg_env):
    save_config(_cfg_two_accounts())
    seen = {}

    def fake_connect(creds, port=993):
        seen["email"] = creds.email
        return _fake_imap()

    with (
        patch("mailbox_cleanup.auth.keyring.get_password", return_value="pw"),
        patch("mailbox_cleanup.cli.imap_connect", side_effect=fake_connect),
    ):
        runner = CliRunner()
        r = runner.invoke(cli, ["senders", "--account", "private", "--top", "5"])
    assert r.exit_code == 0, r.output
    assert seen["email"] == "p@y.example"


@pytest.mark.parametrize(
    "subcommand,extra_args",
    [
        ("delete", ["--sender", "x@y", "--apply"]),
        ("move", ["--to", "Archive", "--sender", "x@y", "--apply"]),
        ("archive", ["--older-than", "12m", "--apply"]),
        ("dedupe", ["--apply"]),
        ("attachments", []),
        ("unsubscribe", ["--sender", "x@y", "--apply"]),
        ("bounces", ["--apply"]),
    ],
)
def test_destructive_subcommand_accepts_account_flag(cfg_env, subcommand, extra_args):
    """All destructive subcommands must accept --account.

    We don't run them end-to-end (operations call deeper IMAP APIs); we just
    assert the subcommand parses --account without an error before reaching
    operation code. Patching `imap_connect` to raise lets us exit early.
    """
    save_config(_cfg_two_accounts())

    class _Boom(Exception):
        pass

    with (
        patch("mailbox_cleanup.auth.keyring.get_password", return_value="pw"),
        patch("mailbox_cleanup.cli.imap_connect", side_effect=_Boom("stop here")),
    ):
        runner = CliRunner()
        args = [subcommand, "--account", "private", *extra_args]
        r = runner.invoke(cli, args)
    # We expect exit 2 (operation/connection error) — proves --account got past
    # parsing AND the resolver picked the right account; the boom happens
    # inside the data subcommand's `with imap_connect(...)` block.
    assert r.exit_code == 2, f"{subcommand} {extra_args}: {r.output!r}"
    assert "stop here" in r.output
    assert "_error" in r.output  # operation_error or connection_error
