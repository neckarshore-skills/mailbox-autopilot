import json
from unittest.mock import patch

import pytest

from mailbox_cleanup.config import (
    DEFAULT_CONFIG_PATH_ENV,
    Account,
    AccountResolutionError,
    Config,
    ConfigError,
    bootstrap_from_v01_keychain,
    config_path,
    derive_alias_from_email,
    derive_provider,
    load_config,
    resolve_account,
    save_config,
    validate_config,
)


def test_derive_provider_ionos():
    assert derive_provider("imap.ionos.de") == "ionos"
    assert derive_provider("imap.ionos.com") == "ionos"


def test_derive_provider_gmail():
    assert derive_provider("imap.gmail.com") == "gmail"
    assert derive_provider("imap.googlemail.com") == "gmail"


def test_derive_provider_icloud():
    assert derive_provider("imap.mail.me.com") == "icloud"
    assert derive_provider("imap.icloud.com") == "icloud"


def test_derive_provider_generic():
    assert derive_provider("mail.example.com") == "generic"
    assert derive_provider("imap.fastmail.com") == "generic"


def test_derive_provider_case_insensitive():
    assert derive_provider("IMAP.IONOS.DE") == "ionos"


def test_derive_alias_simple():
    assert derive_alias_from_email("owner@example.com") == "owner"


def test_derive_alias_with_dot():
    assert derive_alias_from_email("first.last@example.com") == "first-last"


def test_derive_alias_with_plus():
    assert derive_alias_from_email("user+tag@example.com") == "user-tag"


def test_derive_alias_with_underscore():
    assert derive_alias_from_email("a_b@example.com") == "a_b"


def test_derive_alias_uppercase():
    assert derive_alias_from_email("Own3R@EXAMPLE.com") == "own3r"


def test_derive_alias_invalid_email_raises():
    import pytest

    from mailbox_cleanup.config import ConfigError

    with pytest.raises(ConfigError):
        derive_alias_from_email("no-at-sign")


def test_derive_alias_strips_leading_non_alnum():
    assert derive_alias_from_email("-foo@x.example") == "foo"


def test_derive_alias_underscores_only_falls_back():
    assert derive_alias_from_email("___@x.example") == "account"


def test_derive_alias_dots_only_falls_back():
    assert derive_alias_from_email("....@x.example") == "account"


def test_derive_alias_digits_preserved():
    assert derive_alias_from_email("0a@x.example") == "0a"


def test_derive_alias_leading_digit_kept():
    assert derive_alias_from_email("123abc@x.example") == "123abc"


def test_derive_alias_unicode_stripped():
    """Non-ASCII is dropped (documented behavior; ASCII-only aliases for v0.2)."""
    assert derive_alias_from_email("müller@x.example") == "mller"


def test_account_dataclass_defaults():
    a = Account(alias="work", email="x@y.example", server="imap.ionos.de")
    assert a.port == 993
    assert a.provider == "ionos"  # auto-derived


def test_account_explicit_provider_wins():
    a = Account(
        alias="weird",
        email="x@y.example",
        server="some.host.tld",
        provider="custom",
    )
    assert a.provider == "custom"


def test_validate_config_happy_path():
    data = {
        "schema_version": 1,
        "default": "work",
        "accounts": [{"alias": "work", "email": "a@b.example", "server": "imap.ionos.de"}],
    }
    cfg = validate_config(data)
    assert isinstance(cfg, Config)
    assert cfg.default == "work"
    assert len(cfg.accounts) == 1
    assert cfg.accounts[0].alias == "work"


def test_validate_config_empty_default_null_ok():
    data = {"schema_version": 1, "default": None, "accounts": []}
    cfg = validate_config(data)
    assert cfg.default is None
    assert cfg.accounts == ()


def test_validate_config_unknown_default_raises():
    data = {
        "schema_version": 1,
        "default": "missing",
        "accounts": [{"alias": "work", "email": "a@b.example", "server": "x"}],
    }
    with pytest.raises(ConfigError, match="default"):
        validate_config(data)


def test_validate_config_duplicate_alias_raises():
    data = {
        "schema_version": 1,
        "default": "work",
        "accounts": [
            {"alias": "work", "email": "a@b.example", "server": "x"},
            {"alias": "work", "email": "c@d.example", "server": "y"},
        ],
    }
    with pytest.raises(ConfigError, match="duplicate.*alias"):
        validate_config(data)


def test_validate_config_duplicate_email_raises():
    data = {
        "schema_version": 1,
        "default": "a",
        "accounts": [
            {"alias": "a", "email": "x@y.example", "server": "s"},
            {"alias": "b", "email": "x@y.example", "server": "s"},
        ],
    }
    with pytest.raises(ConfigError, match="duplicate.*email"):
        validate_config(data)


def test_validate_config_bad_alias_regex_raises():
    data = {
        "schema_version": 1,
        "default": "Bad-Alias",
        "accounts": [{"alias": "Bad-Alias", "email": "a@b.example", "server": "x"}],
    }
    with pytest.raises(ConfigError, match="alias"):
        validate_config(data)


def test_validate_config_alias_too_long_raises():
    long_alias = "a" * 33  # exceeds 1-32 bound
    data = {
        "schema_version": 1,
        "default": long_alias,
        "accounts": [{"alias": long_alias, "email": "a@b.example", "server": "x"}],
    }
    with pytest.raises(ConfigError, match="alias"):
        validate_config(data)


def test_validate_config_alias_max_length_ok():
    max_alias = "a" + ("0" * 31)  # exactly 32 chars
    data = {
        "schema_version": 1,
        "default": max_alias,
        "accounts": [{"alias": max_alias, "email": "a@b.example", "server": "x"}],
    }
    cfg = validate_config(data)
    assert cfg.accounts[0].alias == max_alias


def test_validate_config_unsupported_schema_version_raises():
    data = {"schema_version": 99, "default": None, "accounts": []}
    with pytest.raises(ConfigError, match="schema_version"):
        validate_config(data)


def test_validate_config_email_at_required():
    data = {
        "schema_version": 1,
        "default": "a",
        "accounts": [{"alias": "a", "email": "no-at", "server": "x"}],
    }
    with pytest.raises(ConfigError, match="email"):
        validate_config(data)


def test_validate_config_missing_required_account_field_raises():
    data = {
        "schema_version": 1,
        "default": None,
        "accounts": [{"alias": "a", "email": "a@b.example"}],  # no server
    }
    with pytest.raises(ConfigError, match="server"):
        validate_config(data)


def test_validate_config_root_must_be_dict_raises():
    with pytest.raises(ConfigError, match="object|dict"):
        validate_config(["not", "a", "dict"])  # type: ignore[arg-type]


def test_validate_config_accounts_must_be_list_raises():
    data = {"schema_version": 1, "default": None, "accounts": "not-a-list"}
    with pytest.raises(ConfigError, match="accounts"):
        validate_config(data)


def test_default_config_path_env_override(monkeypatch, tmp_path):
    target = tmp_path / "elsewhere" / "config.json"
    monkeypatch.setenv(DEFAULT_CONFIG_PATH_ENV, str(target))
    assert config_path() == target


def test_save_and_load_roundtrip(tmp_path, monkeypatch):
    p = tmp_path / "cfg" / "config.json"
    monkeypatch.setenv(DEFAULT_CONFIG_PATH_ENV, str(p))
    cfg = Config(
        default="work",
        accounts=(Account(alias="work", email="a@b.example", server="imap.ionos.de"),),
    )
    save_config(cfg)
    assert p.exists()
    loaded = load_config()
    assert loaded.default == "work"
    assert loaded.accounts[0].alias == "work"
    assert loaded.accounts[0].provider == "ionos"


def test_save_config_sets_secure_mode(tmp_path, monkeypatch):
    p = tmp_path / "cfg" / "config.json"
    monkeypatch.setenv(DEFAULT_CONFIG_PATH_ENV, str(p))
    save_config(Config(default=None, accounts=()))
    assert oct(p.stat().st_mode & 0o777) == oct(0o600)
    assert oct(p.parent.stat().st_mode & 0o777) == oct(0o700)


def test_load_config_missing_raises(tmp_path, monkeypatch):
    p = tmp_path / "nope.json"
    monkeypatch.setenv(DEFAULT_CONFIG_PATH_ENV, str(p))
    with pytest.raises(FileNotFoundError):
        load_config()


def test_load_config_corrupt_json_raises(tmp_path, monkeypatch):
    p = tmp_path / "cfg.json"
    p.write_text("{not valid json")
    monkeypatch.setenv(DEFAULT_CONFIG_PATH_ENV, str(p))
    with pytest.raises(ConfigError, match="parse"):
        load_config()


def test_save_config_atomic_write(tmp_path, monkeypatch):
    p = tmp_path / "cfg.json"
    monkeypatch.setenv(DEFAULT_CONFIG_PATH_ENV, str(p))
    save_config(Config(default=None, accounts=()))
    leftovers = list(tmp_path.glob("*.tmp"))
    assert leftovers == [], f"unexpected temp files: {leftovers}"
    save_config(
        Config(
            default="x",
            accounts=(Account(alias="x", email="a@b.example", server="imap.ionos.de"),),
        )
    )
    data = json.loads(p.read_text())
    assert data["default"] == "x"


def test_save_config_serialises_all_account_fields(tmp_path, monkeypatch):
    """Round-trip preserves alias, email, server, port, provider, schema_version."""
    p = tmp_path / "cfg.json"
    monkeypatch.setenv(DEFAULT_CONFIG_PATH_ENV, str(p))
    cfg = Config(
        default="work",
        accounts=(
            Account(
                alias="work",
                email="a@b.example",
                server="imap.gmail.com",
                port=12345,
                provider="custom",
            ),
        ),
    )
    save_config(cfg)
    data = json.loads(p.read_text())
    assert data["schema_version"] == 1
    assert data["default"] == "work"
    assert data["accounts"][0]["port"] == 12345
    assert data["accounts"][0]["provider"] == "custom"


def _cfg(*aliases, default=None):
    accounts = tuple(
        Account(alias=a, email=f"{a}@x.example", server="imap.ionos.de") for a in aliases
    )
    return Config(default=default, accounts=accounts)


def test_resolve_by_flag_alias():
    cfg = _cfg("work", "private", default="work")
    assert resolve_account(cfg, flag="private", env=None).alias == "private"


def test_resolve_by_flag_email():
    cfg = _cfg("work", "private", default="work")
    assert resolve_account(cfg, flag="private@x.example", env=None).alias == "private"


def test_resolve_by_env_when_no_flag():
    cfg = _cfg("work", "private", default="work")
    assert resolve_account(cfg, flag=None, env="private").alias == "private"


def test_resolve_flag_beats_env():
    cfg = _cfg("work", "private", default="work")
    assert resolve_account(cfg, flag="work", env="private").alias == "work"


def test_resolve_falls_back_to_default():
    cfg = _cfg("work", "private", default="work")
    assert resolve_account(cfg, flag=None, env=None).alias == "work"


def test_resolve_falls_back_to_single_account():
    cfg = _cfg("only", default=None)
    assert resolve_account(cfg, flag=None, env=None).alias == "only"


def test_resolve_no_accounts_raises():
    cfg = _cfg(default=None)
    with pytest.raises(AccountResolutionError, match="no_account_selected"):
        resolve_account(cfg, flag=None, env=None)


def test_resolve_multiple_no_default_no_flag_raises():
    cfg = _cfg("a", "b", default=None)
    with pytest.raises(AccountResolutionError, match="no_account_selected"):
        resolve_account(cfg, flag=None, env=None)


def test_resolve_unknown_account_raises():
    cfg = _cfg("work", default="work")
    with pytest.raises(AccountResolutionError, match="unknown_account"):
        resolve_account(cfg, flag="nope", env=None)


def test_resolve_unknown_env_raises():
    """env-var lookup also raises unknown_account when no match."""
    cfg = _cfg("work", default="work")
    with pytest.raises(AccountResolutionError, match="unknown_account"):
        resolve_account(cfg, flag=None, env="nope")


def test_resolve_inconsistent_default_asserts():
    """Edge case: cfg.default points at a non-existent alias (would normally fail
    validate_config; if reached, resolver hits an internal-invariant assertion)."""
    cfg = Config(
        default="ghost",
        accounts=(Account(alias="real", email="r@x.example", server="imap.ionos.de"),),
    )
    with pytest.raises(AssertionError, match="invariant violated"):
        resolve_account(cfg, flag=None, env=None)


def test_resolve_empty_string_flag_treated_as_none():
    cfg = _cfg("only", default="only")
    assert resolve_account(cfg, flag="", env=None).alias == "only"


def test_resolve_empty_string_env_treated_as_none():
    cfg = _cfg("only", default="only")
    assert resolve_account(cfg, flag=None, env="").alias == "only"


def test_resolution_error_carries_error_code():
    cfg = _cfg("a", "b", default=None)
    with pytest.raises(AccountResolutionError) as exc_info:
        resolve_account(cfg, flag=None, env=None)
    assert exc_info.value.error_code == "no_account_selected"


def test_bootstrap_creates_config_for_known_email(tmp_path, monkeypatch):
    p = tmp_path / "cfg.json"
    monkeypatch.setenv(DEFAULT_CONFIG_PATH_ENV, str(p))
    fake_kr = {
        ("mailbox-cleanup", "owner@example.com"): "secret",
        ("mailbox-cleanup", "imap-server:owner@example.com"): "imap.ionos.de",
    }

    def fake_get(service, key):
        return fake_kr.get((service, key))

    def fake_delete(service, key):
        fake_kr.pop((service, key), None)

    with patch("mailbox_cleanup.config.keyring") as kr:
        kr.get_password.side_effect = fake_get
        kr.delete_password.side_effect = fake_delete
        cfg = bootstrap_from_v01_keychain("owner@example.com")

    assert cfg.default == "owner"
    assert cfg.accounts[0].alias == "owner"
    assert cfg.accounts[0].email == "owner@example.com"
    assert cfg.accounts[0].server == "imap.ionos.de"
    assert cfg.accounts[0].provider == "ionos"
    loaded = load_config()
    assert loaded.default == "owner"
    assert ("mailbox-cleanup", "imap-server:owner@example.com") not in fake_kr
    # password entry preserved
    assert ("mailbox-cleanup", "owner@example.com") in fake_kr


def test_bootstrap_unknown_email_raises(tmp_path, monkeypatch):
    p = tmp_path / "cfg.json"
    monkeypatch.setenv(DEFAULT_CONFIG_PATH_ENV, str(p))
    with patch("mailbox_cleanup.config.keyring") as kr:
        kr.get_password.return_value = None
        with pytest.raises(ConfigError, match="no v0.1 credentials"):
            bootstrap_from_v01_keychain("nobody@x.example")
    # Config file must NOT have been written on failure
    assert not p.exists()


def test_bootstrap_default_server_when_imap_server_key_missing(tmp_path, monkeypatch):
    p = tmp_path / "cfg.json"
    monkeypatch.setenv(DEFAULT_CONFIG_PATH_ENV, str(p))
    fake_kr = {("mailbox-cleanup", "user@x.example"): "pw"}

    def fake_get(service, key):
        return fake_kr.get((service, key))

    with patch("mailbox_cleanup.config.keyring") as kr:
        kr.get_password.side_effect = fake_get
        kr.delete_password.return_value = None
        cfg = bootstrap_from_v01_keychain("user@x.example")

    assert cfg.accounts[0].server == "imap.ionos.de"
    assert cfg.accounts[0].provider == "ionos"


def test_bootstrap_keyring_delete_failure_is_swallowed(tmp_path, monkeypatch):
    """Keyring backends raise varied exceptions on delete; bootstrap must succeed
    even if the obsolete imap-server key cannot be removed."""
    p = tmp_path / "cfg.json"
    monkeypatch.setenv(DEFAULT_CONFIG_PATH_ENV, str(p))
    fake_kr = {("mailbox-cleanup", "user@x.example"): "pw"}

    def fake_get(service, key):
        return fake_kr.get((service, key))

    def boom(service, key):
        raise RuntimeError("keyring backend exploded")

    with patch("mailbox_cleanup.config.keyring") as kr:
        kr.get_password.side_effect = fake_get
        kr.delete_password.side_effect = boom
        cfg = bootstrap_from_v01_keychain("user@x.example")

    assert cfg.accounts[0].alias == "user"
    assert load_config().default == "user"


def test_the_shipped_config_example_is_valid_and_neutral():
    """docs/config.example.json (#74): loads with the real validator, example data only."""
    import json as _json
    from pathlib import Path as _Path

    path = _Path(__file__).parent.parent / "docs" / "config.example.json"
    cfg = validate_config(_json.loads(path.read_text(encoding="utf-8")))
    assert [a.alias for a in cfg.accounts] == ["work", "private"]
    assert cfg.accounts[0].keep == ("billing@example.org", "@bank.example")
    for a in cfg.accounts:
        assert a.email.endswith(("@example.com", "@example.net", "@example.org"))
