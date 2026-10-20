import json

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


def test_config_init_creates_empty_config(cfg_env):
    runner = CliRunner()
    r = runner.invoke(cli, ["config", "init"])
    assert r.exit_code == 0, r.output
    cfg = load_config()
    assert cfg.default is None
    assert cfg.accounts == ()


def test_config_init_idempotent(cfg_env):
    runner = CliRunner()
    runner.invoke(cli, ["config", "init"])
    r = runner.invoke(cli, ["config", "init"])
    assert r.exit_code == 0
    assert "already exists" in r.output.lower()


def test_config_init_with_import_email_bootstraps(cfg_env, monkeypatch):
    """`config init --import-email=<email>` runs the v0.1 bootstrap path."""
    fake_kr = {
        ("mailbox-cleanup", "german@rauhut.com"): "secret",
        ("mailbox-cleanup", "imap-server:german@rauhut.com"): "imap.ionos.de",
    }
    monkeypatch.setattr(
        "mailbox_cleanup.config.keyring.get_password",
        lambda s, k: fake_kr.get((s, k)),
    )
    monkeypatch.setattr(
        "mailbox_cleanup.config.keyring.delete_password",
        lambda s, k: fake_kr.pop((s, k), None),
    )
    runner = CliRunner()
    r = runner.invoke(
        cli,
        ["config", "init", "--import-email", "german@rauhut.com"],
    )
    assert r.exit_code == 0, r.output
    cfg = load_config()
    assert cfg.default == "german"
    assert cfg.accounts[0].email == "german@rauhut.com"


def test_config_init_with_import_email_no_credentials_fails(cfg_env, monkeypatch):
    monkeypatch.setattr("mailbox_cleanup.config.keyring.get_password", lambda s, k: None)
    runner = CliRunner()
    r = runner.invoke(
        cli,
        ["config", "init", "--import-email", "nobody@x.de"],
    )
    assert r.exit_code != 0
    assert "bootstrap_failed" in r.output


def test_config_list_json(cfg_env):
    save_config(
        Config(
            default="work",
            accounts=(
                Account(alias="work", email="a@b.de", server="imap.ionos.de"),
                Account(alias="private", email="c@d.de", server="imap.ionos.de"),
            ),
        )
    )
    runner = CliRunner()
    r = runner.invoke(cli, ["config", "list", "--json"])
    assert r.exit_code == 0
    payload = json.loads(r.output)
    assert payload["default"] == "work"
    assert len(payload["accounts"]) == 2
    assert payload["schema_version"] == 1


def test_config_list_text(cfg_env):
    save_config(
        Config(
            default="work",
            accounts=(Account(alias="work", email="a@b.de", server="imap.ionos.de"),),
        )
    )
    runner = CliRunner()
    r = runner.invoke(cli, ["config", "list"])
    assert r.exit_code == 0
    assert "work" in r.output
    assert "a@b.de" in r.output


def test_config_list_no_config_fails(cfg_env):
    runner = CliRunner()
    r = runner.invoke(cli, ["config", "list", "--json"])
    assert r.exit_code != 0
    assert "no_config" in r.output


def test_config_show_default(cfg_env):
    save_config(
        Config(
            default="work",
            accounts=(Account(alias="work", email="a@b.de", server="imap.ionos.de"),),
        )
    )
    runner = CliRunner()
    r = runner.invoke(cli, ["config", "show", "--json"])
    assert r.exit_code == 0
    data = json.loads(r.output)
    assert data["alias"] == "work"


def test_config_show_specific_alias(cfg_env):
    save_config(
        Config(
            default="work",
            accounts=(
                Account(alias="work", email="a@b.de", server="imap.ionos.de"),
                Account(alias="private", email="c@d.de", server="imap.ionos.de"),
            ),
        )
    )
    runner = CliRunner()
    r = runner.invoke(cli, ["config", "show", "private", "--json"])
    data = json.loads(r.output)
    assert data["alias"] == "private"


def test_config_show_unknown_alias_fails(cfg_env):
    save_config(
        Config(
            default="work",
            accounts=(Account(alias="work", email="a@b.de", server="imap.ionos.de"),),
        )
    )
    runner = CliRunner()
    r = runner.invoke(cli, ["config", "show", "nope", "--json"])
    assert r.exit_code != 0
    assert "unknown_account" in r.output


def test_config_show_no_alias_no_default_fails(cfg_env):
    save_config(
        Config(
            default=None,
            accounts=(
                Account(alias="a", email="a@x.de", server="imap.ionos.de"),
                Account(alias="b", email="b@x.de", server="imap.ionos.de"),
            ),
        )
    )
    runner = CliRunner()
    r = runner.invoke(cli, ["config", "show", "--json"])
    assert r.exit_code != 0
    assert "no_account_selected" in r.output


def test_config_set_default(cfg_env):
    save_config(
        Config(
            default="work",
            accounts=(
                Account(alias="work", email="a@b.de", server="imap.ionos.de"),
                Account(alias="private", email="c@d.de", server="imap.ionos.de"),
            ),
        )
    )
    runner = CliRunner()
    r = runner.invoke(cli, ["config", "set-default", "private"])
    assert r.exit_code == 0
    assert load_config().default == "private"


def test_config_set_default_unknown_fails(cfg_env):
    save_config(
        Config(
            default="work",
            accounts=(Account(alias="work", email="a@b.de", server="imap.ionos.de"),),
        )
    )
    runner = CliRunner()
    r = runner.invoke(cli, ["config", "set-default", "nope"])
    assert r.exit_code != 0
    assert "unknown_account" in r.output


def test_config_rename(cfg_env):
    save_config(
        Config(
            default="work",
            accounts=(Account(alias="work", email="a@b.de", server="imap.ionos.de"),),
        )
    )
    runner = CliRunner()
    r = runner.invoke(cli, ["config", "rename", "work", "office"])
    assert r.exit_code == 0
    cfg = load_config()
    assert cfg.accounts[0].alias == "office"
    assert cfg.default == "office"  # default updated to follow rename


def test_config_rename_unknown_old_fails(cfg_env):
    save_config(
        Config(
            default="work",
            accounts=(Account(alias="work", email="a@b.de", server="imap.ionos.de"),),
        )
    )
    runner = CliRunner()
    r = runner.invoke(cli, ["config", "rename", "ghost", "office"])
    assert r.exit_code != 0
    assert "unknown_account" in r.output


def test_config_rename_to_existing_fails(cfg_env):
    save_config(
        Config(
            default="work",
            accounts=(
                Account(alias="work", email="a@b.de", server="imap.ionos.de"),
                Account(alias="private", email="c@d.de", server="imap.ionos.de"),
            ),
        )
    )
    runner = CliRunner()
    r = runner.invoke(cli, ["config", "rename", "work", "private"])
    assert r.exit_code != 0
    assert "duplicate_alias" in r.output


@pytest.fixture
def two_accounts(cfg_env, monkeypatch):
    """Two aliases; Keychain deletions are recorded instead of performed."""
    save_config(
        Config(
            default="work",
            accounts=(
                Account(alias="work", email="a@b.example", server="imap.ionos.de"),
                Account(alias="private", email="c@d.example", server="imap.ionos.de"),
            ),
        )
    )
    deleted = []
    monkeypatch.setattr(
        "mailbox_cleanup.auth.keyring.delete_password", lambda s, k: deleted.append(k)
    )
    return deleted


def test_config_remove_keeps_the_keychain_entry_by_default(two_accounts):
    """#75: removing an alias no longer destroys the credential."""
    r = CliRunner().invoke(cli, ["config", "remove", "private"])
    assert r.exit_code == 0, r.output
    assert [a.alias for a in load_config().accounts] == ["work"]
    assert two_accounts == []
    assert "kept" in r.output.lower()
    assert "--delete-credentials" in r.output


def test_config_remove_deletes_credentials_only_when_asked_and_confirmed(two_accounts):
    r = CliRunner().invoke(
        cli, ["config", "remove", "private", "--delete-credentials"], input="y\n"
    )
    assert r.exit_code == 0, r.output
    assert [a.alias for a in load_config().accounts] == ["work"]
    assert any("c@d.example" in str(k) for k in two_accounts)


def test_config_remove_declined_confirmation_changes_nothing(two_accounts):
    r = CliRunner().invoke(
        cli, ["config", "remove", "private", "--delete-credentials"], input="n\n"
    )
    assert r.exit_code != 0
    assert [a.alias for a in load_config().accounts] == ["work", "private"]
    assert two_accounts == []


def test_config_remove_without_a_terminal_answer_changes_nothing(two_accounts):
    """Claude's Bash has no stdin: EOF at the prompt must abort, not delete."""
    r = CliRunner().invoke(cli, ["config", "remove", "private", "--delete-credentials"], input="")
    assert r.exit_code != 0
    assert [a.alias for a in load_config().accounts] == ["work", "private"]
    assert two_accounts == []


def test_config_remove_refuses_credential_deletion_non_interactively(two_accounts):
    r = CliRunner().invoke(
        cli,
        ["config", "remove", "private", "--delete-credentials", "--non-interactive"],
        input="y\n",
    )
    assert r.exit_code == 4, r.output
    assert "confirmation_required" in r.output
    assert [a.alias for a in load_config().accounts] == ["work", "private"]
    assert two_accounts == []


def test_config_remove_default_clears_default_field(cfg_env, monkeypatch):
    save_config(
        Config(
            default="work",
            accounts=(
                Account(alias="work", email="a@b.de", server="imap.ionos.de"),
                Account(alias="private", email="c@d.de", server="imap.ionos.de"),
            ),
        )
    )
    monkeypatch.setattr(
        "mailbox_cleanup.auth.keyring.delete_password",
        lambda s, k: None,
    )
    runner = CliRunner()
    r = runner.invoke(cli, ["config", "remove", "work"])
    assert r.exit_code == 0
    cfg = load_config()
    assert cfg.default is None


def test_config_remove_unknown_fails(cfg_env):
    save_config(
        Config(
            default="work",
            accounts=(Account(alias="work", email="a@b.de", server="imap.ionos.de"),),
        )
    )
    runner = CliRunner()
    r = runner.invoke(cli, ["config", "remove", "ghost"])
    assert r.exit_code != 0
    assert "unknown_account" in r.output
