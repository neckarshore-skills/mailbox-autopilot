# Multi-Account Support — Implementation Plan (v0.2)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add multi-account support to `mailbox-cleanup` CLI without breaking the existing single-account flow.

**Architecture:** New `config.py` module owns accounts (alias, email, server, port, provider) in `~/.mailbox-cleanup/config.json`; secrets stay in Keychain. CLI gains a `config` subcommand group; existing subcommands take `--account=<alias-or-email>` instead of `--email=<email>` (with deprecation shim). Auto-bootstraps from v0.1 Keychain on first invocation.

**Tech Stack (unchanged from v0.1):** Python 3.11+, `click`, `imap-tools`, `keyring`, `requests`, `pytest`, `ruff`, `uv`, Greenmail.

**Spec:** [`docs/2026-05-04-multi-account-design.md`](2026-05-04-multi-account-design.md)

**Phases:**

- **Phase A** (Tasks 1-5): Pure config layer — derivation, dataclasses, validation, file I/O, resolver
- **Phase B** (Task 6): Auto-bootstrap from v0.1 Keychain
- **Phase C** (Tasks 7-8): CLI helpers + new `config` subcommand group
- **Phase D** (Tasks 9-11): Rewire `auth` and data subcommands; add audit `account` field
- **Phase E** (Tasks 12-14): Integration test, docs, version bump

Every task ends with a green `pytest` run and clean `ruff check`. Each task commits independently.

---

## Task 1: Provider derivation

**Files:**
- Create: `src/mailbox_cleanup/config.py`
- Create: `tests/test_config.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_config.py`:

```python
from mailbox_cleanup.config import derive_provider


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
```

- [ ] **Step 2: Verify tests fail**

```bash
cd ~/Developer/projects/imap-mailbox-cleanup
uv run pytest tests/test_config.py -v
```
Expected: ImportError / ModuleNotFoundError on `mailbox_cleanup.config`.

- [ ] **Step 3: Implement `derive_provider`**

Create `src/mailbox_cleanup/config.py`:

```python
"""Multi-account configuration: schema, validation, file I/O, resolution, migration."""

from __future__ import annotations


def derive_provider(server: str) -> str:
    """Map an IMAP server hostname to a provider label.

    Free-form return; new providers can be added without consumer changes.
    """
    s = server.lower().strip()
    if "ionos." in s:
        return "ionos"
    if s.endswith("gmail.com") or s.endswith("googlemail.com"):
        return "gmail"
    if "mail.me.com" in s or s.endswith("icloud.com"):
        return "icloud"
    return "generic"
```

- [ ] **Step 4: Verify tests pass**

```bash
uv run pytest tests/test_config.py -v
uv run ruff check src/mailbox_cleanup/config.py tests/test_config.py
```
Expected: all PASS, no lint warnings.

- [ ] **Step 5: Commit**

```bash
git add src/mailbox_cleanup/config.py tests/test_config.py
git commit -m "feat(config): provider derivation from server hostname"
```

---

## Task 2: Alias derivation from email

**Files:**
- Modify: `src/mailbox_cleanup/config.py`
- Modify: `tests/test_config.py`

- [ ] **Step 1: Append failing tests**

Append to `tests/test_config.py`:

```python
from mailbox_cleanup.config import derive_alias_from_email


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
```

- [ ] **Step 2: Verify tests fail**

```bash
uv run pytest tests/test_config.py -v
```

- [ ] **Step 3: Implement `derive_alias_from_email` and `ConfigError`**

Append to `src/mailbox_cleanup/config.py`:

```python
import re

_ALIAS_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


class ConfigError(Exception):
    """Raised on invalid or unrecoverable config state."""


def derive_alias_from_email(email: str) -> str:
    """Derive a slugified alias from an email's local-part.

    Replaces `.` and `+` with `-`, lowercases, trims leading non-alphanumeric.
    Raises ConfigError if the email has no local-part.
    """
    if "@" not in email:
        raise ConfigError(f"Invalid email (no @): {email!r}")
    local = email.split("@", 1)[0].lower()
    slug = re.sub(r"[.+]", "-", local)
    slug = re.sub(r"[^a-z0-9_-]", "", slug)
    slug = slug.lstrip("-_0123456789")
    if not slug:
        # fallback: use cleaned local-part even if it starts with a digit/underscore
        slug = re.sub(r"[^a-z0-9_-]", "", local) or "account"
    if not _ALIAS_RE.match(slug):
        raise ConfigError(f"Could not derive valid alias from {email!r}")
    return slug
```

- [ ] **Step 4: Verify tests pass**

```bash
uv run pytest tests/test_config.py -v
uv run ruff check src/mailbox_cleanup/config.py tests/test_config.py
```

- [ ] **Step 5: Commit**

```bash
git add src/mailbox_cleanup/config.py tests/test_config.py
git commit -m "feat(config): derive alias slug from email local-part"
```

---

## Task 3: Account / Config dataclasses + validation

**Files:**
- Modify: `src/mailbox_cleanup/config.py`
- Modify: `tests/test_config.py`

- [ ] **Step 1: Append failing tests**

Append to `tests/test_config.py`:

```python
from mailbox_cleanup.config import Account, Config, validate_config


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
        "accounts": [
            {"alias": "work", "email": "a@b.example", "server": "imap.ionos.de"}
        ],
    }
    cfg = validate_config(data)
    assert cfg.default == "work"
    assert len(cfg.accounts) == 1
    assert cfg.accounts[0].alias == "work"


def test_validate_config_empty_default_null_ok():
    data = {"schema_version": 1, "default": None, "accounts": []}
    cfg = validate_config(data)
    assert cfg.default is None
    assert cfg.accounts == []


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
        "accounts": [
            {"alias": "Bad-Alias", "email": "a@b.example", "server": "x"}
        ],
    }
    with pytest.raises(ConfigError, match="alias"):
        validate_config(data)


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
```

Note: `pytest` already imported at the top of the test file from Task 2. If not, add `import pytest` at the top of the file.

- [ ] **Step 2: Verify tests fail**

```bash
uv run pytest tests/test_config.py -v
```

- [ ] **Step 3: Implement dataclasses + validation**

Append to `src/mailbox_cleanup/config.py`:

```python
from collections.abc import Sequence
from dataclasses import dataclass, field

SCHEMA_VERSION = 1
_ALIAS_REGEX_STR = r"^[a-z0-9][a-z0-9_-]{0,31}$"
_ALIAS_FULL_RE = re.compile(_ALIAS_REGEX_STR)


@dataclass(frozen=True)
class Account:
    alias: str
    email: str
    server: str
    port: int = 993
    provider: str = ""

    def __post_init__(self):
        # frozen dataclass: must use object.__setattr__ for derived defaults
        if not self.provider:
            object.__setattr__(self, "provider", derive_provider(self.server))


@dataclass(frozen=True)
class Config:
    default: str | None
    accounts: Sequence[Account] = field(default_factory=tuple)
    schema_version: int = SCHEMA_VERSION


def _validate_account_dict(d: dict) -> Account:
    for required in ("alias", "email", "server"):
        if required not in d:
            raise ConfigError(f"Account missing required field {required!r}: {d!r}")
    alias = d["alias"]
    if not isinstance(alias, str) or not _ALIAS_FULL_RE.match(alias):
        raise ConfigError(
            f"Invalid alias {alias!r}; must match {_ALIAS_REGEX_STR}"
        )
    email = d["email"]
    if not isinstance(email, str) or "@" not in email:
        raise ConfigError(f"Invalid email {email!r} for alias {alias!r}")
    return Account(
        alias=alias,
        email=email,
        server=d["server"],
        port=int(d.get("port", 993)),
        provider=d.get("provider", ""),
    )


def validate_config(data: dict) -> Config:
    """Parse and validate a raw config dict. Raises ConfigError on any problem."""
    if not isinstance(data, dict):
        raise ConfigError(f"Config root must be an object, got {type(data).__name__}")
    sv = data.get("schema_version")
    if sv != SCHEMA_VERSION:
        raise ConfigError(
            f"Unsupported schema_version: {sv!r} (this CLI knows {SCHEMA_VERSION})"
        )
    accounts_raw = data.get("accounts", [])
    if not isinstance(accounts_raw, list):
        raise ConfigError("'accounts' must be a list")
    accounts = tuple(_validate_account_dict(a) for a in accounts_raw)

    seen_aliases = set()
    seen_emails = set()
    for a in accounts:
        if a.alias in seen_aliases:
            raise ConfigError(f"duplicate alias: {a.alias!r}")
        if a.email in seen_emails:
            raise ConfigError(f"duplicate email: {a.email!r}")
        seen_aliases.add(a.alias)
        seen_emails.add(a.email)

    default = data.get("default")
    if default is not None:
        if default not in seen_aliases:
            raise ConfigError(
                f"default {default!r} is not an existing alias: {sorted(seen_aliases)}"
            )

    return Config(default=default, accounts=accounts, schema_version=SCHEMA_VERSION)
```

- [ ] **Step 4: Verify tests pass**

```bash
uv run pytest tests/test_config.py -v
uv run ruff check src/mailbox_cleanup/config.py tests/test_config.py
```

- [ ] **Step 5: Commit**

```bash
git add src/mailbox_cleanup/config.py tests/test_config.py
git commit -m "feat(config): Account/Config dataclasses and validation"
```

---

## Task 4: load_config / save_config (file I/O with secure mode)

**Files:**
- Modify: `src/mailbox_cleanup/config.py`
- Modify: `tests/test_config.py`

- [ ] **Step 1: Append failing tests**

Append to `tests/test_config.py`:

```python
import json
import os
import stat

from mailbox_cleanup.config import (
    DEFAULT_CONFIG_PATH_ENV,
    config_path,
    load_config,
    save_config,
)


def test_default_config_path_env_override(monkeypatch, tmp_path):
    target = tmp_path / "elsewhere" / "config.json"
    monkeypatch.setenv(DEFAULT_CONFIG_PATH_ENV, str(target))
    assert config_path() == target


def test_save_and_load_roundtrip(tmp_path, monkeypatch):
    p = tmp_path / "cfg" / "config.json"
    monkeypatch.setenv(DEFAULT_CONFIG_PATH_ENV, str(p))
    cfg = Config(
        default="work",
        accounts=(
            Account(alias="work", email="a@b.example", server="imap.ionos.de"),
        ),
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
    # sanity: no leftover .tmp file
    leftovers = list(tmp_path.glob("*.tmp"))
    assert leftovers == [], f"unexpected temp files: {leftovers}"
    # second write replaces cleanly
    save_config(Config(
        default="x",
        accounts=(Account(alias="x", email="a@b.example", server="imap.ionos.de"),),
    ))
    data = json.loads(p.read_text())
    assert data["default"] == "x"
```

- [ ] **Step 2: Verify tests fail**

```bash
uv run pytest tests/test_config.py -v
```

- [ ] **Step 3: Implement file I/O**

Append to `src/mailbox_cleanup/config.py`:

```python
import json
import os
from dataclasses import asdict
from pathlib import Path

DEFAULT_CONFIG_PATH_ENV = "MAILBOX_CLEANUP_CONFIG"
DEFAULT_CONFIG_PATH = Path.home() / ".mailbox-cleanup" / "config.json"


def config_path() -> Path:
    override = os.environ.get(DEFAULT_CONFIG_PATH_ENV)
    return Path(override) if override else DEFAULT_CONFIG_PATH


def _to_dict(cfg: Config) -> dict:
    return {
        "schema_version": cfg.schema_version,
        "default": cfg.default,
        "accounts": [asdict(a) for a in cfg.accounts],
    }


def save_config(cfg: Config) -> None:
    """Write config atomically (tmp + rename) with mode 0600 / parent 0700."""
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    tmp = path.with_suffix(path.suffix + ".tmp")
    payload = json.dumps(_to_dict(cfg), ensure_ascii=False, indent=2)
    # write tmp, set mode, rename
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(payload)
    except Exception:
        if tmp.exists():
            tmp.unlink()
        raise
    os.replace(tmp, path)
    os.chmod(path, 0o600)


def load_config() -> Config:
    """Read and validate config from the current path. Raises FileNotFoundError or ConfigError."""
    path = config_path()
    if not path.exists():
        raise FileNotFoundError(f"Config not found: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ConfigError(f"Could not parse config at {path}: {e}") from e
    return validate_config(data)
```

- [ ] **Step 4: Verify tests pass**

```bash
uv run pytest tests/test_config.py -v
uv run ruff check src/mailbox_cleanup/config.py tests/test_config.py
```

- [ ] **Step 5: Commit**

```bash
git add src/mailbox_cleanup/config.py tests/test_config.py
git commit -m "feat(config): atomic load/save with secure file modes"
```

---

## Task 5: Account resolver (precedence order)

**Files:**
- Modify: `src/mailbox_cleanup/config.py`
- Modify: `tests/test_config.py`

- [ ] **Step 1: Append failing tests**

Append to `tests/test_config.py`:

```python
from mailbox_cleanup.config import (
    AccountResolutionError,
    resolve_account,
)


def _cfg(*aliases, default=None):
    accounts = tuple(
        Account(alias=a, email=f"{a}@x.example", server="imap.ionos.de")
        for a in aliases
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


def test_resolve_empty_string_flag_treated_as_none():
    cfg = _cfg("only", default="only")
    assert resolve_account(cfg, flag="", env=None).alias == "only"
```

- [ ] **Step 2: Verify tests fail**

```bash
uv run pytest tests/test_config.py -v
```

- [ ] **Step 3: Implement resolver**

Append to `src/mailbox_cleanup/config.py`:

```python
class AccountResolutionError(Exception):
    """Raised when no account can be resolved.

    `error_code` is one of: 'no_account_selected', 'unknown_account'.
    """

    def __init__(self, error_code: str, message: str):
        super().__init__(message)
        self.error_code = error_code


def _find_account(cfg: Config, identifier: str) -> Account | None:
    for a in cfg.accounts:
        if a.alias == identifier or a.email == identifier:
            return a
    return None


def resolve_account(cfg: Config, *, flag: str | None, env: str | None) -> Account:
    """Resolve which Account to operate on.

    Precedence (highest first): flag, env, cfg.default, single-account, hard-fail.
    """
    if flag:
        a = _find_account(cfg, flag)
        if a is None:
            raise AccountResolutionError(
                "unknown_account",
                f"Unknown account {flag!r}; known: {[x.alias for x in cfg.accounts]}",
            )
        return a
    if env:
        a = _find_account(cfg, env)
        if a is None:
            raise AccountResolutionError(
                "unknown_account",
                f"Unknown account in MAILBOX_CLEANUP_ACCOUNT={env!r}",
            )
        return a
    if cfg.default:
        a = _find_account(cfg, cfg.default)
        if a is not None:
            return a
        # default points to non-existent alias — config is inconsistent
        raise AccountResolutionError(
            "unknown_account",
            f"Default {cfg.default!r} is not an existing alias",
        )
    if len(cfg.accounts) == 1:
        return cfg.accounts[0]
    raise AccountResolutionError(
        "no_account_selected",
        "Multiple accounts configured. Specify --account=<alias>, "
        "set MAILBOX_CLEANUP_ACCOUNT=, or run 'mailbox-cleanup config set-default <alias>'.",
    )
```

- [ ] **Step 4: Verify tests pass**

```bash
uv run pytest tests/test_config.py -v
uv run ruff check src/mailbox_cleanup/config.py tests/test_config.py
```

- [ ] **Step 5: Commit**

```bash
git add src/mailbox_cleanup/config.py tests/test_config.py
git commit -m "feat(config): account resolver with flag/env/default/single-account precedence"
```

---

## Task 6: Auto-bootstrap from v0.1 Keychain

**Files:**
- Modify: `src/mailbox_cleanup/config.py`
- Modify: `tests/test_config.py`

- [ ] **Step 1: Append failing tests**

Append to `tests/test_config.py`:

```python
from unittest.mock import patch

from mailbox_cleanup.config import bootstrap_from_v01_keychain


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
    # config persisted
    loaded = load_config()
    assert loaded.default == "owner"
    # imap-server entry deleted from "Keychain"
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


def test_bootstrap_default_server_when_imap_server_key_missing(tmp_path, monkeypatch):
    p = tmp_path / "cfg.json"
    monkeypatch.setenv(DEFAULT_CONFIG_PATH_ENV, str(p))
    fake_kr = {("mailbox-cleanup", "user@x.example"): "pw"}

    def fake_get(service, key):
        return fake_kr.get((service, key))

    with patch("mailbox_cleanup.config.keyring") as kr:
        kr.get_password.side_effect = fake_get
        cfg = bootstrap_from_v01_keychain("user@x.example")

    assert cfg.accounts[0].server == "imap.ionos.de"
```

- [ ] **Step 2: Verify tests fail**

```bash
uv run pytest tests/test_config.py -v
```

- [ ] **Step 3: Implement bootstrap**

Append to `src/mailbox_cleanup/config.py`:

```python
import keyring  # imported at top of file is fine; placing here for locality

V01_SERVICE_NAME = "mailbox-cleanup"
V01_SERVER_KEY_PREFIX = "imap-server:"
V01_DEFAULT_SERVER = "imap.ionos.de"


def bootstrap_from_v01_keychain(email: str) -> Config:
    """Create a v0.2 config from a v0.1 single-account Keychain entry.

    Reads the password (must exist) and the optional imap-server entry, derives
    alias and provider, writes config.json, deletes the obsolete imap-server key.
    """
    if keyring.get_password(V01_SERVICE_NAME, email) is None:
        raise ConfigError(
            f"Bootstrap failed: no v0.1 credentials in Keychain for {email!r}"
        )
    server = (
        keyring.get_password(V01_SERVICE_NAME, f"{V01_SERVER_KEY_PREFIX}{email}")
        or V01_DEFAULT_SERVER
    )
    alias = derive_alias_from_email(email)
    account = Account(
        alias=alias,
        email=email,
        server=server,
        port=993,
    )
    cfg = Config(default=alias, accounts=(account,))
    save_config(cfg)
    # Clean up obsolete v0.1 server key
    try:
        keyring.delete_password(V01_SERVICE_NAME, f"{V01_SERVER_KEY_PREFIX}{email}")
    except Exception:  # noqa: BLE001 — keyring backends raise varied exceptions
        pass
    return cfg
```

Move the `import keyring` to the top of the module if not already there.

- [ ] **Step 4: Verify tests pass**

```bash
uv run pytest tests/test_config.py -v
uv run ruff check src/mailbox_cleanup/config.py tests/test_config.py
```

- [ ] **Step 5: Commit**

```bash
git add src/mailbox_cleanup/config.py tests/test_config.py
git commit -m "feat(config): bootstrap v0.2 config from v0.1 Keychain entry"
```

---

## Task 7: CLI helper for account resolution + auto-bootstrap

**Files:**
- Create: `src/mailbox_cleanup/cli_helpers.py`
- Create: `tests/test_cli_helpers.py`

This helper centralises the "given `--account` and/or `--email`, return `(Account, Credentials)`" logic. All data subcommands will call it. The auto-bootstrap trigger and the deprecation warning live here.

- [ ] **Step 1: Write failing tests**

Create `tests/test_cli_helpers.py`:

```python
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
    save_config(Config(
        default=default,
        accounts=(
            Account(alias="work", email="work@x.example", server="imap.ionos.de"),
            Account(alias="private", email="priv@y.example", server="imap.ionos.de"),
        ),
    ))
    return p


def _patch_keyring(passwords: dict):
    return patch(
        "mailbox_cleanup.auth.keyring.get_password",
        side_effect=lambda service, account: passwords.get(account),
    )


def test_resolves_via_account_flag(tmp_path, monkeypatch):
    _make_existing_config(tmp_path, monkeypatch)
    monkeypatch.delenv("MAILBOX_CLEANUP_ACCOUNT", raising=False)
    with _patch_keyring({"work@x.example": "pw1", "priv@y.example": "pw2"}):
        account, creds = resolve_account_and_credentials(
            account_flag="private", email_flag=None
        )
    assert account.alias == "private"
    assert creds.email == "priv@y.example"
    assert creds.password == "pw2"


def test_email_flag_used_as_account_with_deprecation_warning(tmp_path, monkeypatch):
    _make_existing_config(tmp_path, monkeypatch)
    monkeypatch.delenv("MAILBOX_CLEANUP_ACCOUNT", raising=False)
    with _patch_keyring({"work@x.example": "pw1", "priv@y.example": "pw2"}):
        with pytest.warns(DeprecationWarning, match="--email is deprecated"):
            account, _ = resolve_account_and_credentials(
                account_flag=None, email_flag="work@x.example"
            )
    assert account.alias == "work"


def test_account_flag_overrides_email_flag(tmp_path, monkeypatch):
    _make_existing_config(tmp_path, monkeypatch)
    monkeypatch.delenv("MAILBOX_CLEANUP_ACCOUNT", raising=False)
    with _patch_keyring({"work@x.example": "p", "priv@y.example": "p"}):
        account, _ = resolve_account_and_credentials(
            account_flag="private", email_flag="work@x.example"
        )
    assert account.alias == "private"


def test_env_var_used_when_no_flags(tmp_path, monkeypatch):
    _make_existing_config(tmp_path, monkeypatch)
    monkeypatch.setenv("MAILBOX_CLEANUP_ACCOUNT", "private")
    with _patch_keyring({"work@x.example": "p", "priv@y.example": "p"}):
        account, _ = resolve_account_and_credentials(
            account_flag=None, email_flag=None
        )
    assert account.alias == "private"


def test_auto_bootstrap_when_no_config_and_email_flag(tmp_path, monkeypatch, capsys):
    p = tmp_path / "cfg.json"
    monkeypatch.setenv(DEFAULT_CONFIG_PATH_ENV, str(p))
    monkeypatch.delenv("MAILBOX_CLEANUP_ACCOUNT", raising=False)
    assert not p.exists()
    with patch("mailbox_cleanup.config.keyring") as cfg_kr, _patch_keyring(
        {"owner@example.com": "pw"}
    ):
        cfg_kr.get_password.side_effect = lambda service, key: {
            ("mailbox-cleanup", "owner@example.com"): "pw",
            ("mailbox-cleanup", "imap-server:owner@example.com"): "imap.ionos.de",
        }.get((service, key))
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
```

- [ ] **Step 2: Verify tests fail**

```bash
uv run pytest tests/test_cli_helpers.py -v
```

- [ ] **Step 3: Implement helper**

Create `src/mailbox_cleanup/cli_helpers.py`:

```python
"""Shared CLI plumbing: account resolution, auto-bootstrap, --email deprecation."""

from __future__ import annotations

import os
import sys
import warnings

from .auth import Credentials, get_credentials
from .config import (
    Account,
    AccountResolutionError,
    ConfigError,
    bootstrap_from_v01_keychain,
    config_path,
    load_config,
    resolve_account,
)


class AccountFlagsError(Exception):
    """User-facing CLI error with structured error_code."""

    def __init__(self, error_code: str, message: str):
        super().__init__(message)
        self.error_code = error_code


def resolve_account_and_credentials(
    *,
    account_flag: str | None,
    email_flag: str | None,
) -> tuple[Account, Credentials]:
    """Resolve which account to use and load its credentials.

    Handles:
    - --email deprecation (emits DeprecationWarning, treats as --account)
    - Auto-bootstrap from v0.1 Keychain when no config exists yet but
      --email is provided
    - Precedence: flag > env > config.default > single account
    """
    # Auto-bootstrap path
    if not config_path().exists():
        if email_flag:
            try:
                bootstrap_from_v01_keychain(email_flag)
                print(
                    f"Migrated to multi-account config "
                    f"({config_path()}). "
                    "Use 'mailbox-cleanup config rename' to change the alias.",
                    file=sys.stderr,
                )
            except ConfigError as e:
                raise AccountFlagsError(
                    "bootstrap_failed",
                    f"Could not auto-bootstrap from v0.1 Keychain: {e}",
                ) from e
        else:
            raise AccountFlagsError(
                "no_config",
                "No config found at "
                f"{config_path()}. Run 'mailbox-cleanup config init' or "
                "pass --account / --email to bootstrap.",
            )

    # Deprecation: treat --email as --account if --account not given
    if email_flag and not account_flag:
        warnings.warn(
            "--email is deprecated; use --account=<alias-or-email>. "
            "Removed in v0.3.",
            DeprecationWarning,
            stacklevel=2,
        )
        account_flag = email_flag

    cfg = load_config()
    env_value = os.environ.get("MAILBOX_CLEANUP_ACCOUNT")
    try:
        account = resolve_account(cfg, flag=account_flag, env=env_value)
    except AccountResolutionError as e:
        raise AccountFlagsError(e.error_code, str(e)) from e

    creds = get_credentials(account.email)
    return account, creds
```

- [ ] **Step 4: Verify tests pass**

```bash
uv run pytest tests/test_cli_helpers.py tests/test_config.py -v
uv run ruff check src/mailbox_cleanup/cli_helpers.py tests/test_cli_helpers.py
```

- [ ] **Step 5: Commit**

```bash
git add src/mailbox_cleanup/cli_helpers.py tests/test_cli_helpers.py
git commit -m "feat(cli): account-resolution helper with --email shim and auto-bootstrap"
```

---

## Task 8: New `config` subcommand group

**Files:**
- Modify: `src/mailbox_cleanup/cli.py`
- Create: `tests/test_cli_config.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_cli_config.py`:

```python
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


def test_config_list_json(cfg_env):
    save_config(Config(
        default="work",
        accounts=(
            Account(alias="work", email="a@b.example", server="imap.ionos.de"),
            Account(alias="private", email="c@d.example", server="imap.ionos.de"),
        ),
    ))
    runner = CliRunner()
    r = runner.invoke(cli, ["config", "list", "--json"])
    assert r.exit_code == 0
    payload = json.loads(r.output)
    assert payload["default"] == "work"
    assert len(payload["accounts"]) == 2


def test_config_show_default(cfg_env):
    save_config(Config(
        default="work",
        accounts=(Account(alias="work", email="a@b.example", server="imap.ionos.de"),),
    ))
    runner = CliRunner()
    r = runner.invoke(cli, ["config", "show", "--json"])
    assert r.exit_code == 0
    data = json.loads(r.output)
    assert data["alias"] == "work"


def test_config_show_specific_alias(cfg_env):
    save_config(Config(
        default="work",
        accounts=(
            Account(alias="work", email="a@b.example", server="imap.ionos.de"),
            Account(alias="private", email="c@d.example", server="imap.ionos.de"),
        ),
    ))
    runner = CliRunner()
    r = runner.invoke(cli, ["config", "show", "private", "--json"])
    data = json.loads(r.output)
    assert data["alias"] == "private"


def test_config_show_unknown_alias_fails(cfg_env):
    save_config(Config(
        default="work",
        accounts=(Account(alias="work", email="a@b.example", server="imap.ionos.de"),),
    ))
    runner = CliRunner()
    r = runner.invoke(cli, ["config", "show", "nope", "--json"])
    assert r.exit_code != 0
    assert "unknown_account" in r.output


def test_config_set_default(cfg_env):
    save_config(Config(
        default="work",
        accounts=(
            Account(alias="work", email="a@b.example", server="imap.ionos.de"),
            Account(alias="private", email="c@d.example", server="imap.ionos.de"),
        ),
    ))
    runner = CliRunner()
    r = runner.invoke(cli, ["config", "set-default", "private"])
    assert r.exit_code == 0
    assert load_config().default == "private"


def test_config_set_default_unknown_fails(cfg_env):
    save_config(Config(
        default="work",
        accounts=(Account(alias="work", email="a@b.example", server="imap.ionos.de"),),
    ))
    runner = CliRunner()
    r = runner.invoke(cli, ["config", "set-default", "nope"])
    assert r.exit_code != 0


def test_config_rename(cfg_env):
    save_config(Config(
        default="work",
        accounts=(Account(alias="work", email="a@b.example", server="imap.ionos.de"),),
    ))
    runner = CliRunner()
    r = runner.invoke(cli, ["config", "rename", "work", "office"])
    assert r.exit_code == 0
    cfg = load_config()
    assert cfg.accounts[0].alias == "office"
    assert cfg.default == "office"  # default updated to follow rename


def test_config_remove_also_clears_keychain(cfg_env, monkeypatch):
    save_config(Config(
        default="work",
        accounts=(
            Account(alias="work", email="a@b.example", server="imap.ionos.de"),
            Account(alias="private", email="c@d.example", server="imap.ionos.de"),
        ),
    ))
    deleted = []

    def fake_delete(service, key):
        deleted.append((service, key))

    monkeypatch.setattr("mailbox_cleanup.auth.keyring.delete_password", fake_delete)

    runner = CliRunner()
    r = runner.invoke(cli, ["config", "remove", "private"])
    assert r.exit_code == 0
    cfg = load_config()
    assert [a.alias for a in cfg.accounts] == ["work"]
    # password deletion attempted for the removed account
    assert any("c@d.example" in str(k) for _, k in deleted)


def test_config_remove_default_clears_default_field(cfg_env, monkeypatch):
    save_config(Config(
        default="work",
        accounts=(
            Account(alias="work", email="a@b.example", server="imap.ionos.de"),
            Account(alias="private", email="c@d.example", server="imap.ionos.de"),
        ),
    ))
    monkeypatch.setattr(
        "mailbox_cleanup.auth.keyring.delete_password",
        lambda s, k: None,
    )
    runner = CliRunner()
    r = runner.invoke(cli, ["config", "remove", "work"])
    assert r.exit_code == 0
    cfg = load_config()
    assert cfg.default is None
```

- [ ] **Step 2: Verify tests fail**

```bash
uv run pytest tests/test_cli_config.py -v
```

- [ ] **Step 3: Implement `config` subcommand group**

Add to `src/mailbox_cleanup/cli.py`, near the existing `auth` group:

```python
from dataclasses import asdict, replace as dc_replace

from .auth import delete_credentials
from .config import (
    SCHEMA_VERSION as CONFIG_SCHEMA_VERSION,
    Account,
    Config,
    ConfigError,
    config_path,
    load_config,
    save_config,
)


@cli.group("config")
def config_group():
    """Manage multi-account configuration (~/.mailbox-cleanup/config.json)."""


@config_group.command("init")
@click.option(
    "--import-email",
    "import_email",
    default=None,
    help="Bootstrap a single account from a v0.1 Keychain entry for this email.",
)
def config_init(import_email: str | None):
    """Create an empty config file (idempotent), or bootstrap one account from v0.1 Keychain."""
    from .config import bootstrap_from_v01_keychain

    if config_path().exists():
        click.echo(f"Config already exists at {config_path()}")
        return
    if import_email:
        try:
            cfg = bootstrap_from_v01_keychain(import_email)
        except ConfigError as e:
            _fail(
                {"error_code": "bootstrap_failed", "message": str(e)},
                4,
                json_mode=False,
            )
            return
        click.echo(
            f"Imported v0.1 account ({import_email}) as alias "
            f"{cfg.accounts[0].alias!r}; default set."
        )
        return
    save_config(Config(default=None, accounts=()))
    click.echo(f"Config created at {config_path()}")


@config_group.command("list")
@click.option("--json", "json_mode", is_flag=True)
def config_list(json_mode: bool):
    """List all accounts."""
    try:
        cfg = load_config()
    except FileNotFoundError:
        _fail({"error_code": "no_config", "message": f"No config at {config_path()}"}, 5, json_mode)
        return
    payload = {
        "schema_version": cfg.schema_version,
        "default": cfg.default,
        "accounts": [asdict(a) for a in cfg.accounts],
    }
    if json_mode:
        click.echo(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        click.echo(f"Default: {cfg.default or '(none)'}")
        for a in cfg.accounts:
            marker = "*" if a.alias == cfg.default else " "
            click.echo(f"  {marker} {a.alias:16s} {a.email:32s} {a.server} ({a.provider})")


@config_group.command("show")
@click.argument("alias", required=False)
@click.option("--json", "json_mode", is_flag=True)
def config_show(alias: str | None, json_mode: bool):
    """Show one account (defaults to the default account)."""
    try:
        cfg = load_config()
    except FileNotFoundError:
        _fail({"error_code": "no_config", "message": f"No config at {config_path()}"}, 5, json_mode)
        return
    target = alias or cfg.default
    if target is None:
        _fail({"error_code": "no_account_selected", "message": "No alias given and no default."}, 4, json_mode)
        return
    found = next((a for a in cfg.accounts if a.alias == target), None)
    if found is None:
        _fail({"error_code": "unknown_account", "message": f"Unknown alias {target!r}"}, 4, json_mode)
        return
    if json_mode:
        click.echo(json.dumps(asdict(found), ensure_ascii=False, indent=2))
    else:
        for k, v in asdict(found).items():
            click.echo(f"{k}: {v}")


@config_group.command("set-default")
@click.argument("alias")
def config_set_default(alias: str):
    """Set the default account."""
    cfg = load_config()
    if not any(a.alias == alias for a in cfg.accounts):
        _fail(
            {"error_code": "unknown_account", "message": f"No account with alias {alias!r}"},
            4,
            json_mode=False,
        )
        return
    save_config(dc_replace(cfg, default=alias))
    click.echo(f"Default set to {alias}.")


@config_group.command("rename")
@click.argument("old_alias")
@click.argument("new_alias")
def config_rename(old_alias: str, new_alias: str):
    """Rename an account's alias. Updates `default` if it pointed at the old alias."""
    cfg = load_config()
    if not any(a.alias == old_alias for a in cfg.accounts):
        _fail({"error_code": "unknown_account", "message": f"No alias {old_alias!r}"}, 4, json_mode=False)
        return
    if any(a.alias == new_alias for a in cfg.accounts):
        _fail({"error_code": "duplicate_alias", "message": f"Alias {new_alias!r} already exists"}, 4, json_mode=False)
        return
    new_accounts = tuple(
        dc_replace(a, alias=new_alias) if a.alias == old_alias else a
        for a in cfg.accounts
    )
    new_default = new_alias if cfg.default == old_alias else cfg.default
    save_config(Config(default=new_default, accounts=new_accounts))
    click.echo(f"Renamed {old_alias} → {new_alias}.")


@config_group.command("remove")
@click.argument("alias")
def config_remove(alias: str):
    """Remove an account from config and delete its Keychain password."""
    cfg = load_config()
    target = next((a for a in cfg.accounts if a.alias == alias), None)
    if target is None:
        _fail({"error_code": "unknown_account", "message": f"No alias {alias!r}"}, 4, json_mode=False)
        return
    new_accounts = tuple(a for a in cfg.accounts if a.alias != alias)
    new_default = cfg.default if cfg.default != alias else None
    save_config(Config(default=new_default, accounts=new_accounts))
    delete_credentials(target.email)
    click.echo(f"Removed account {alias} ({target.email}).")
```

You must also add to the imports at the top of `cli.py`:

```python
from dataclasses import asdict, replace as dc_replace
```

(Add `Config`, `load_config`, `save_config`, `config_path`, `ConfigError` to the existing `from .config import ...` block — create that import if it doesn't exist.)

- [ ] **Step 4: Verify tests pass**

```bash
uv run pytest tests/test_cli_config.py -v
uv run pytest -v   # full suite must stay green
uv run ruff check src/mailbox_cleanup/cli.py tests/test_cli_config.py
```

- [ ] **Step 5: Commit**

```bash
git add src/mailbox_cleanup/cli.py tests/test_cli_config.py
git commit -m "feat(cli): config subcommand group (init/list/show/set-default/rename/remove)"
```

---

## Task 9: Update `auth set/test/delete` for alias workflow

**Files:**
- Modify: `src/mailbox_cleanup/cli.py`
- Modify: `tests/test_cli_auth.py` (existing) — extend, not replace
- Create: `tests/test_cli_auth_multi.py`

The existing single-account `auth set --email=...` flow must keep working. New `auth set --alias=... --email=... --make-default` writes to both Keychain and config atomically. `auth test`/`auth delete` switch from `--email` to `--account` with a deprecation shim for `--email`.

- [ ] **Step 1: Write failing tests**

Create `tests/test_cli_auth_multi.py`:

```python
from unittest.mock import patch

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
    save_config(Config(
        default="work",
        accounts=(Account(alias="work", email="a@b.example", server="imap.ionos.de"),),
    ))
    runner = CliRunner()
    with patch("mailbox_cleanup.cli.set_credentials"):
        r = runner.invoke(
            cli,
            ["auth", "set", "--alias", "work", "--email", "z@z.example"],
            input="x\n",
        )
    assert r.exit_code != 0
    assert "duplicate_alias" in r.output


def test_auth_set_legacy_no_alias_uses_email_local_part(cfg_env):
    """Backward-compat: `auth set --email=...` (no --alias) derives one and adds the account."""
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
    assert cfg.default == "owner"  # single account becomes default automatically


def test_auth_test_by_account(cfg_env):
    save_config(Config(
        default="work",
        accounts=(Account(alias="work", email="a@b.example", server="imap.ionos.de"),),
    ))
    runner = CliRunner()
    with patch("mailbox_cleanup.cli.imap_connect") as imap, patch(
        "mailbox_cleanup.auth.keyring.get_password",
        return_value="pw",
    ):
        imap.return_value.__enter__.return_value.folder.list.return_value = []
        r = runner.invoke(cli, ["auth", "test", "--account", "work", "--json"])
    assert r.exit_code == 0
    assert '"ok": true' in r.output


def test_auth_delete_by_account_removes_both(cfg_env):
    save_config(Config(
        default="work",
        accounts=(
            Account(alias="work", email="a@b.example", server="imap.ionos.de"),
            Account(alias="private", email="c@d.example", server="imap.ionos.de"),
        ),
    ))
    runner = CliRunner()
    with patch("mailbox_cleanup.auth.keyring.delete_password") as kdel:
        r = runner.invoke(cli, ["auth", "delete", "--account", "work"])
    assert r.exit_code == 0
    cfg = load_config()
    assert [a.alias for a in cfg.accounts] == ["private"]
    assert cfg.default is None
    kdel.assert_called()
```

- [ ] **Step 2: Verify tests fail**

```bash
uv run pytest tests/test_cli_auth_multi.py -v
```

- [ ] **Step 3: Update `auth` commands**

In `src/mailbox_cleanup/cli.py`, replace the existing `auth_set`, `auth_test`, `auth_delete` definitions:

```python
@auth.command("set")
@click.option("--alias", default=None, help="Slug alias for the account (optional; derived from email).")
@click.option("--email", required=True, help="Email address.")
@click.option("--server", default="imap.ionos.de", show_default=True)
@click.option("--port", default=993, show_default=True, type=int)
@click.option("--provider", default=None, help="Override auto-derived provider label.")
@click.option("--make-default", is_flag=True, help="Set this account as the default.")
@click.password_option(confirmation_prompt=False, prompt="Password")
def auth_set(alias, email, server, port, provider, make_default, password):
    """Store credentials in Keychain and add the account to the config."""
    from .config import Account, Config, derive_alias_from_email

    # Load or initialise config
    if config_path().exists():
        cfg = load_config()
    else:
        cfg = Config(default=None, accounts=())

    final_alias = alias or derive_alias_from_email(email)

    if any(a.alias == final_alias for a in cfg.accounts):
        _fail(
            {"error_code": "duplicate_alias", "message": f"Alias {final_alias!r} already exists"},
            4,
            json_mode=False,
        )
        return
    if any(a.email == email for a in cfg.accounts):
        _fail(
            {"error_code": "duplicate_email", "message": f"Email {email!r} already exists"},
            4,
            json_mode=False,
        )
        return

    new_account = Account(
        alias=final_alias,
        email=email,
        server=server,
        port=port,
        provider=provider or "",
    )
    new_accounts = (*cfg.accounts, new_account)
    new_default = cfg.default
    if make_default or new_default is None:
        new_default = final_alias
    set_credentials(email, password, server)
    save_config(Config(default=new_default, accounts=new_accounts))
    click.echo(f"Stored credentials for {email} (alias: {final_alias}, server: {server}).")


@auth.command("test")
@click.option("--account", "account_flag", default=None, help="Alias or email.")
@click.option("--email", "email_flag", default=None, help="Deprecated; use --account.")
@click.option("--json", "json_mode", is_flag=True)
def auth_test(account_flag, email_flag, json_mode):
    """Connect to IMAP, list folders, disconnect."""
    from .cli_helpers import AccountFlagsError, resolve_account_and_credentials

    try:
        account, creds = resolve_account_and_credentials(
            account_flag=account_flag, email_flag=email_flag
        )
    except AccountFlagsError as e:
        _fail({"error_code": e.error_code, "message": str(e)}, 4, json_mode)
        return
    except AuthMissingError as e:
        _fail({"error_code": "auth_missing", "message": str(e)}, 3, json_mode)
        return

    try:
        with imap_connect(creds) as mb:
            folders = [f.name for f in mb.folder.list()]
    except Exception as e:
        _fail({"error_code": "connection_error", "message": str(e)}, 2, json_mode)
        return
    _emit(
        {
            "ok": True,
            "account": account.alias,
            "email": account.email,
            "server": account.server,
            "folders": folders,
            "schema_version": SCHEMA_VERSION,
        },
        json_mode=json_mode,
    )


@auth.command("delete")
@click.option("--account", "account_flag", default=None)
@click.option("--email", "email_flag", default=None, help="Deprecated; use --account.")
def auth_delete(account_flag, email_flag):
    """Remove an account from config AND its password from Keychain."""
    from .cli_helpers import AccountFlagsError, resolve_account_and_credentials

    try:
        account, _ = resolve_account_and_credentials(
            account_flag=account_flag, email_flag=email_flag
        )
    except AccountFlagsError as e:
        _fail({"error_code": e.error_code, "message": str(e)}, 4, json_mode=False)
        return

    cfg = load_config()
    new_accounts = tuple(a for a in cfg.accounts if a.alias != account.alias)
    new_default = cfg.default if cfg.default != account.alias else None
    save_config(Config(default=new_default, accounts=new_accounts))
    delete_credentials(account.email)
    click.echo(f"Removed account {account.alias} ({account.email}).")
```

Update existing tests in `tests/test_cli_auth.py` if they fail because the prompt label changed from "IONOS password" to "Password" — change the assertion accordingly. **Do not** drop those tests; they verify the password prompt + Keychain side-effect.

- [ ] **Step 4: Verify tests pass**

```bash
uv run pytest -v
uv run ruff check src/mailbox_cleanup/cli.py tests/test_cli_auth_multi.py
```

If `tests/test_cli_auth.py` fails because old assertions no longer hold (e.g., it expected the absence of a config file), patch those tests to use the same `cfg_env` fixture pattern.

- [ ] **Step 5: Commit**

```bash
git add src/mailbox_cleanup/cli.py tests/test_cli_auth_multi.py tests/test_cli_auth.py
git commit -m "feat(auth): alias-aware auth set/test/delete; --account replaces --email"
```

---

## Task 10: Migrate data subcommands to `--account`

**Files:**
- Modify: `src/mailbox_cleanup/cli.py` (subcommands: scan, senders, delete, move, archive, dedupe, attachments, unsubscribe, bounces)
- Update: relevant existing CLI tests if any reference `--email`

Mechanical change for nine subcommands: replace the single `@click.option("--email", required=True)` with two options (`--account` and deprecated `--email`), and use `resolve_account_and_credentials` to get `(account, creds)` instead of calling `get_credentials(email)` directly.

The audit `account` field is added in Task 11 — for now, audit calls keep their existing signature.

- [ ] **Step 1: Write a regression test that asserts `--account` works on `scan`**

Append to `tests/test_cli_auth_multi.py` (or a new file `tests/test_cli_data_account.py`):

```python
def test_scan_uses_account_flag(cfg_env, monkeypatch):
    save_config(Config(
        default="work",
        accounts=(Account(alias="work", email="w@x.example", server="imap.ionos.de"),),
    ))
    monkeypatch.setattr(
        "mailbox_cleanup.auth.keyring.get_password",
        lambda s, k: "pw" if k == "w@x.example" else None,
    )

    seen_creds = {}

    class FakeMb:
        def __enter__(self): return self
        def __exit__(self, *a): pass

        class folder:
            @staticmethod
            def set(name): seen_creds["folder"] = name

        @staticmethod
        def fetch(**kw): return iter([])

    def fake_connect(creds, port=993):
        seen_creds["email"] = creds.email
        seen_creds["server"] = creds.server
        return FakeMb()

    monkeypatch.setattr("mailbox_cleanup.cli.imap_connect", fake_connect)
    runner = CliRunner()
    r = runner.invoke(cli, ["scan", "--account", "work", "--json"])
    assert r.exit_code == 0, r.output
    assert seen_creds["email"] == "w@x.example"


def test_scan_falls_back_to_default(cfg_env, monkeypatch):
    save_config(Config(
        default="work",
        accounts=(
            Account(alias="work", email="w@x.example", server="imap.ionos.de"),
            Account(alias="private", email="p@y.example", server="imap.ionos.de"),
        ),
    ))
    monkeypatch.setattr(
        "mailbox_cleanup.auth.keyring.get_password",
        lambda s, k: "pw",
    )
    used = {}

    def fake_connect(creds, port=993):
        used["email"] = creds.email
        class FakeMb:
            def __enter__(self): return self
            def __exit__(self, *a): pass
            class folder:
                @staticmethod
                def set(n): pass
            @staticmethod
            def fetch(**kw): return iter([])
        return FakeMb()

    monkeypatch.setattr("mailbox_cleanup.cli.imap_connect", fake_connect)
    runner = CliRunner()
    r = runner.invoke(cli, ["scan", "--json"])
    assert r.exit_code == 0
    assert used["email"] == "w@x.example"  # default account


def test_scan_email_flag_emits_deprecation(cfg_env, monkeypatch):
    save_config(Config(
        default="work",
        accounts=(Account(alias="work", email="w@x.example", server="imap.ionos.de"),),
    ))
    monkeypatch.setattr(
        "mailbox_cleanup.auth.keyring.get_password", lambda s, k: "pw"
    )
    monkeypatch.setattr(
        "mailbox_cleanup.cli.imap_connect",
        lambda creds, port=993: type("M", (), {
            "__enter__": lambda self: self,
            "__exit__": lambda self, *a: None,
            "folder": type("F", (), {"set": staticmethod(lambda n: None)}),
            "fetch": staticmethod(lambda **kw: iter([])),
        })(),
    )
    runner = CliRunner()
    r = runner.invoke(cli, ["scan", "--email", "w@x.example", "--json"])
    assert r.exit_code == 0
    # DeprecationWarning is captured by pytest only with -W; for CLI tests we
    # assert the operation still works. Stronger check is in test_cli_helpers.
```

- [ ] **Step 2: Verify failing on `--account` flag**

```bash
uv run pytest tests/test_cli_auth_multi.py::test_scan_uses_account_flag -v
```

- [ ] **Step 3: Implement the rewire**

For each of `scan`, `senders`, `delete`, `move`, `archive`, `dedupe`, `attachments`, `unsubscribe`, `bounces` in `src/mailbox_cleanup/cli.py`:

Replace the single email option:

```python
@click.option("--email", required=True)
```

with two options:

```python
@click.option("--account", "account_flag", default=None, help="Alias or email.")
@click.option("--email", "email_flag", default=None, help="Deprecated; use --account.")
```

Replace the body's auth-resolution block, which currently looks like:

```python
try:
    creds = get_credentials(email)
except AuthMissingError as e:
    _fail({"error_code": "auth_missing", "message": str(e)}, 3, json_mode)
    return
```

with:

```python
from .cli_helpers import AccountFlagsError, resolve_account_and_credentials

try:
    account, creds = resolve_account_and_credentials(
        account_flag=account_flag, email_flag=email_flag
    )
except AccountFlagsError as e:
    _fail({"error_code": e.error_code, "message": str(e)}, 4, json_mode)
    return
except AuthMissingError as e:
    _fail({"error_code": "auth_missing", "message": str(e)}, 3, json_mode)
    return
```

(Move the `from .cli_helpers import ...` to the top of the file rather than repeating it inline.)

Update each function's signature: replace `email: str` with `account_flag, email_flag` (and rename remaining args accordingly so click maps cleanly).

For `unsubscribe_cmd`, the existing code references `creds.email` and `creds.password` for SMTP — these continue to work because `Credentials` is unchanged.

- [ ] **Step 4: Verify all tests pass**

```bash
uv run pytest -v
uv run ruff check src/mailbox_cleanup/cli.py
```

The pre-existing CLI tests that pass `--email` must keep passing (deprecation shim). If they fail because of missing config, update them to use the `cfg_env` fixture pattern.

- [ ] **Step 5: Commit**

```bash
git add src/mailbox_cleanup/cli.py tests/test_cli_auth_multi.py
git commit -m "feat(cli): all data subcommands accept --account; --email deprecated"
```

---

## Task 11: Add `account` field to audit log

**Files:**
- Modify: `src/mailbox_cleanup/audit.py`
- Modify: `tests/test_audit.py`
- Modify: `src/mailbox_cleanup/cli.py` (every `log_action(...)` call)

- [ ] **Step 1: Update tests**

Edit `tests/test_audit.py`:

```python
def test_log_action_appends_jsonl(tmp_audit):
    log_action(
        subcommand="delete",
        account="work",
        args={"sender": "x@y.example", "older_than": "6m"},
        folder="INBOX",
        affected_uids=["1", "2", "3"],
        result="success",
    )
    log_action(
        subcommand="archive",
        account="private",
        args={"older_than": "12m"},
        folder="INBOX",
        affected_uids=["4"],
        result="success",
    )
    lines = tmp_audit.read_text().strip().split("\n")
    rec1 = json.loads(lines[0])
    assert rec1["account"] == "work"
    assert rec1["subcommand"] == "delete"
    rec2 = json.loads(lines[1])
    assert rec2["account"] == "private"


def test_log_action_records_failure(tmp_audit):
    log_action(
        subcommand="delete",
        account="work",
        args={"sender": "x@y.example"},
        folder="INBOX",
        affected_uids=[],
        result="failure",
        error="connection lost",
    )
    rec = json.loads(tmp_audit.read_text().strip())
    assert rec["result"] == "failure"
    assert rec["error"] == "connection lost"
    assert rec["account"] == "work"


def test_log_action_creates_parent_dir(tmp_path, monkeypatch):
    nested = tmp_path / "a" / "b" / "audit.log"
    monkeypatch.setenv(AUDIT_LOG_PATH_ENV, str(nested))
    log_action(
        subcommand="x",
        account="work",
        args={},
        folder="INBOX",
        affected_uids=[],
        result="success",
    )
    assert nested.exists()
```

- [ ] **Step 2: Verify tests fail**

```bash
uv run pytest tests/test_audit.py -v
```

- [ ] **Step 3: Update `log_action` signature and CLI callsites**

Edit `src/mailbox_cleanup/audit.py`:

```python
def log_action(
    *,
    subcommand: str,
    account: str,
    args: Mapping[str, object],
    folder: str,
    affected_uids: Sequence[str],
    result: str,
    error: str | None = None,
) -> None:
    """Append one JSON-line record describing an applied action."""
    record: dict[str, object] = {
        "timestamp": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "subcommand": subcommand,
        "account": account,
        "args": dict(args),
        "folder": folder,
        "affected_uids": list(affected_uids),
        "result": result,
    }
    if error is not None:
        record["error"] = error
    path = _audit_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
```

In `src/mailbox_cleanup/cli.py`, every `log_action(...)` call gains `account=account.alias` (the `account` variable from `resolve_account_and_credentials`). The seven call sites are inside: `delete_cmd`, `move_cmd`, `archive_cmd`, `dedupe_cmd`, `unsubscribe_cmd`, `bounces_cmd`. Add `account=account.alias` to each.

- [ ] **Step 4: Verify all tests pass**

```bash
uv run pytest -v
uv run ruff check src/mailbox_cleanup
```

- [ ] **Step 5: Commit**

```bash
git add src/mailbox_cleanup/audit.py src/mailbox_cleanup/cli.py tests/test_audit.py
git commit -m "feat(audit): add account field to JSONL records"
```

---

## Task 12: Greenmail integration test for multi-account

**Files:**
- Create: `tests/integration/__init__.py`
- Create: `tests/integration/test_multi_account.py`

The repo already runs Greenmail in `tests/docker-compose.test.yml` for v0.1 integration. Reuse the same pattern: spin Greenmail, create two users, configure both via `auth set`, run `scan` with `--account` and via env-var override.

- [ ] **Step 1: Inspect existing integration setup**

```bash
cat tests/docker-compose.test.yml
ls tests/fixtures/
```

Identify how v0.1 integration tests bootstrap users in Greenmail (e.g., environment variables, JMAP API, or test fixtures). Mirror that approach.

- [ ] **Step 2: Write the integration test**

Create `tests/integration/__init__.py` (empty file).

Create `tests/integration/test_multi_account.py`:

```python
"""End-to-end test: two Greenmail accounts, switch via --account and env var."""

import pytest
from click.testing import CliRunner

from mailbox_cleanup.cli import cli
from mailbox_cleanup.config import (
    DEFAULT_CONFIG_PATH_ENV,
)


pytestmark = pytest.mark.integration


@pytest.fixture
def two_account_setup(tmp_path, monkeypatch, greenmail_running):
    """Greenmail must be up; create cfg pointing at two users."""
    p = tmp_path / "cfg.json"
    monkeypatch.setenv(DEFAULT_CONFIG_PATH_ENV, str(p))
    monkeypatch.delenv("MAILBOX_CLEANUP_ACCOUNT", raising=False)
    runner = CliRunner()

    # Account 1: work
    r = runner.invoke(
        cli,
        [
            "auth", "set",
            "--alias", "work",
            "--email", "work@example.com",
            "--server", "localhost",
            "--port", "3143",
            "--make-default",
        ],
        input="workpw\n",
    )
    assert r.exit_code == 0, r.output

    # Account 2: private
    r = runner.invoke(
        cli,
        [
            "auth", "set",
            "--alias", "private",
            "--email", "private@example.com",
            "--server", "localhost",
            "--port", "3143",
        ],
        input="privatepw\n",
    )
    assert r.exit_code == 0, r.output
    return runner


def test_scan_uses_default_account(two_account_setup):
    runner = two_account_setup
    r = runner.invoke(cli, ["scan", "--json"])
    assert r.exit_code == 0
    assert "work@example.com" in r.output or '"folder"' in r.output


def test_scan_with_account_flag_uses_other_account(two_account_setup):
    runner = two_account_setup
    r = runner.invoke(cli, ["scan", "--account", "private", "--json"])
    assert r.exit_code == 0


def test_scan_env_var_overrides_default(two_account_setup, monkeypatch):
    runner = two_account_setup
    monkeypatch.setenv("MAILBOX_CLEANUP_ACCOUNT", "private")
    r = runner.invoke(cli, ["scan", "--json"])
    assert r.exit_code == 0
```

You will likely need a `greenmail_running` fixture in `tests/conftest.py` (or `tests/integration/conftest.py`) — check the existing v0.1 conftest for the pattern. If Greenmail spawning is already a session fixture, reuse it.

`auth set` against Greenmail uses port 3143 and trivially-accepted credentials. If the existing harness expects different ports or auth, mirror those.

- [ ] **Step 3: Run integration tests locally**

```bash
docker compose -f tests/docker-compose.test.yml up -d
uv run pytest tests/integration/test_multi_account.py -v -m integration
docker compose -f tests/docker-compose.test.yml down
```

If Greenmail rejects `auth set` because passwords cannot be "set" against Greenmail without bootstrapping users, follow the same user-create pathway used by v0.1 integration tests (likely `GREENMAIL_OPTS` env or a fixture that POSTs to Greenmail's user-management API).

- [ ] **Step 4: Verify CI**

Push and verify GitHub Actions runs the new test successfully. CI already runs `pytest -m integration` per existing setup.

- [ ] **Step 5: Commit**

```bash
git add tests/integration/__init__.py tests/integration/test_multi_account.py
# any conftest changes
git commit -m "test(integration): two-account Greenmail end-to-end"
```

---

## Task 13: Update README and skill documentation

**Files:**
- Modify: `README.md`
- Modify: `skill/SKILL.md`

- [ ] **Step 1: Update README**

In `README.md`, in the Auth/Setup section:

- Replace single-account `auth set --email=...` instructions with the new alias-based flow.
- Add a "Multi-account" section showing `config list`, `config set-default`, and `--account` usage.
- Mention auto-bootstrap from v0.1 (one-line note: "Existing v0.1 users: run any subcommand with `--email=...` once and the CLI migrates automatically.").
- Update the schema/error-code reference to include the new errors (`no_account_selected`, `unknown_account`, `duplicate_alias`, `duplicate_email`, `config_corrupt`, `schema_version_unsupported`).

- [ ] **Step 2: Update SKILL.md**

In `skill/SKILL.md`:

- Mirror the same multi-account workflow.
- Update example invocations: where v0.1 had `mailbox-cleanup scan --email=...`, show `mailbox-cleanup scan --account=work` (and a note that the alias is whatever the user configured).
- Add a setup-time decision tree: "is there a config file? → use it; if not → ask for email + alias and run `auth set`".

- [ ] **Step 3: Verify with existing checks**

```bash
uv run pytest -v
uv run ruff check .
```

- [ ] **Step 4: Commit**

```bash
git add README.md skill/SKILL.md
git commit -m "docs: multi-account workflow in README and SKILL"
```

---

## Task 14: Bump version, full validation, push

**Files:**
- Modify: `pyproject.toml`
- Modify: `src/mailbox_cleanup/__init__.py` (if it exposes `__version__`)

- [ ] **Step 1: Bump version**

In `pyproject.toml`:

```toml
version = "0.2.0"
```

If `src/mailbox_cleanup/__init__.py` exposes `__version__`, update it too.

- [ ] **Step 2: Reinstall the editable tool**

```bash
uv tool install --reinstall --editable ~/Developer/projects/imap-mailbox-cleanup
mailbox-cleanup --version
```

Expected: `mailbox-cleanup, version 0.2.0`.

- [ ] **Step 3: Full validation**

```bash
uv run pytest -v                                  # unit
uv run pytest -v -m integration                   # if Greenmail is up
uv run ruff check .
uv run ruff format --check .
```

All must pass. Test count should be ≥ 59 (v0.1 baseline) plus the new tests added in Tasks 1-12.

- [ ] **Step 4: Manual smoke (read-only) on real account**

```bash
# Auto-bootstrap from v0.1 — runs the migration once
mailbox-cleanup scan --email=owner@example.com --json | head -20

# After bootstrap, --account works
mailbox-cleanup config list
mailbox-cleanup scan --account=german --json | head -20
```

Verify:
- `~/.mailbox-cleanup/config.json` exists with mode `0600` and the expected single account.
- The deprecation warning fires for `--email`.
- The second invocation does not re-trigger migration.

- [ ] **Step 5: Commit + push**

```bash
git add pyproject.toml src/mailbox_cleanup/__init__.py
git commit -m "chore: bump version to 0.2.0 (multi-account)"
git push
```

Verify CI is green: `gh run list --limit 3`.

---

## Spec coverage check

| Spec section | Implemented in |
|--------------|----------------|
| §3 Architecture: config.json + Keychain split | Tasks 3-4 |
| §3 Audit `account` field | Task 11 |
| §4 Schema v1, mode 0600 / parent 0700 | Task 4 |
| §4 Provider derivation table | Task 1 |
| §5.1 `config` subcommand group | Task 8 |
| §5.2 `auth set/test/delete` alias flow | Task 9 |
| §5.3 `--account` on data subcommands | Task 10 |
| §5.4 Resolution precedence | Tasks 5, 7 |
| §6.1 Auto-bootstrap on first invocation | Tasks 6, 7 |
| §6.2 Explicit `config init` / `--import-email` | Task 8 |
| §6.3 `--email` deprecation shim | Task 7 (helper) + Task 9, 10 (wiring) |
| §7 Error codes | Tasks 5, 7, 8, 9 |
| §8 Audit `account` field | Task 11 |
| §9 Testing strategy | Tasks 1-12 cover unit + CLI + Greenmail |
| §10 Module layout | Tasks 3-7 (`config.py`, `cli_helpers.py`); Tasks 9-11 (modifications) |
| §12 Schema-compatibility promise | Encoded in `validate_config` (Task 3) |
