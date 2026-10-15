"""Multi-account configuration: schema, validation, file I/O, resolution, migration."""

from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

import keyring


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


_ALIAS_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")


class ConfigError(Exception):
    """Raised on invalid or unrecoverable config state."""


def derive_alias_from_email(email: str) -> str:
    """Derive a slugified alias from an email's local-part.

    Replaces `.` and `+` with `-`, lowercases, drops other non-alphanumerics,
    and trims leading hyphens/underscores so the result starts with [a-z0-9].
    Falls back to "account" if no usable characters remain.
    Raises ConfigError if the email has no `@`.
    """
    if "@" not in email:
        raise ConfigError(f"Invalid email (no @): {email!r}")
    local = email.split("@", 1)[0].lower()
    slug = re.sub(r"[.+]", "-", local)
    slug = re.sub(r"[^a-z0-9_-]", "", slug)
    slug = slug.lstrip("-_")
    if not slug:
        slug = "account"
    if not _ALIAS_RE.match(slug):  # defensive — should be unreachable
        raise ConfigError(f"Could not derive valid alias from {email!r}")
    return slug


SCHEMA_VERSION = 1


def normalize_keep(entries) -> tuple[str, ...]:
    """Validate keep entries and return them lowercased, de-duplicated, in order.

    An entry is a full address (name@example.com) or a domain (@example.com).
    Anything else is refused: a bare word such as a newsletter's name would have
    to be matched as a substring, and nobody can predict what that keeps.
    Raises ValueError naming the first bad entry.
    """
    out: list[str] = []
    for raw in entries:
        e = (raw or "").strip().lower() if isinstance(raw, str) else None
        if e is None:
            raise ValueError(f"keep entry {raw!r} must be a string")
        _local, at, domain = e.partition("@")
        if not at or not domain or "@" in domain or "." not in domain:
            raise ValueError(
                f"keep entry {raw!r} must be a full address (name@example.com) "
                "or a domain (@example.com)"
            )
        if e not in out:
            out.append(e)
    return tuple(out)


@dataclass(frozen=True)
class Account:
    alias: str
    email: str
    server: str
    port: int = 993
    provider: str = ""
    # Senders never acted on in this account (Founder decision 2026-10-13/-15).
    keep: tuple[str, ...] = ()

    def __post_init__(self):
        # frozen dataclass: must use object.__setattr__ for derived defaults
        if not self.provider:
            object.__setattr__(self, "provider", derive_provider(self.server))


@dataclass(frozen=True)
class Config:
    default: str | None
    accounts: tuple[Account, ...] = field(default_factory=tuple)
    schema_version: int = SCHEMA_VERSION


def _validate_account_dict(d: dict) -> Account:
    for required in ("alias", "email", "server"):
        if required not in d:
            raise ConfigError(f"Account missing required field {required!r}: {d!r}")
    alias = d["alias"]
    if not isinstance(alias, str) or not _ALIAS_RE.match(alias):
        raise ConfigError(
            f"Invalid alias {alias!r}; must match ^[a-z0-9][a-z0-9_-]{{0,31}}$ "
            "(1-32 chars, leading alphanumeric, lowercase)"
        )
    email = d["email"]
    if not isinstance(email, str) or "@" not in email:
        raise ConfigError(f"Invalid email {email!r} for alias {alias!r}")
    keep_raw = d.get("keep", [])
    if not isinstance(keep_raw, list):
        raise ConfigError(f"'keep' of alias {alias!r} must be a list, got {keep_raw!r}")
    try:
        keep = normalize_keep(keep_raw)
    except ValueError as e:
        raise ConfigError(f"Invalid keep list of alias {alias!r}: {e}") from e
    return Account(
        alias=alias,
        email=email,
        server=d["server"],
        port=int(d.get("port", 993)),
        provider=d.get("provider", ""),
        keep=keep,
    )


def validate_config(data: dict) -> Config:
    """Parse and validate a raw config dict. Raises ConfigError on any problem."""
    if not isinstance(data, dict):
        raise ConfigError(f"Config root must be an object, got {type(data).__name__}")
    sv = data.get("schema_version")
    if sv != SCHEMA_VERSION:
        raise ConfigError(f"Unsupported schema_version: {sv!r} (this CLI knows {SCHEMA_VERSION})")
    accounts_raw = data.get("accounts", [])
    if not isinstance(accounts_raw, list):
        raise ConfigError("'accounts' must be a list")
    accounts = tuple(_validate_account_dict(a) for a in accounts_raw)

    seen_aliases: set[str] = set()
    seen_emails: set[str] = set()
    for a in accounts:
        if a.alias in seen_aliases:
            raise ConfigError(f"duplicate alias: {a.alias!r}")
        if a.email in seen_emails:
            raise ConfigError(f"duplicate email: {a.email!r}")
        seen_aliases.add(a.alias)
        seen_emails.add(a.email)

    default = data.get("default")
    if default is not None and default not in seen_aliases:
        raise ConfigError(f"default {default!r} is not an existing alias: {sorted(seen_aliases)}")

    return Config(default=default, accounts=accounts, schema_version=SCHEMA_VERSION)


DEFAULT_CONFIG_PATH_ENV = "MAILBOX_CLEANUP_CONFIG"
DEFAULT_CONFIG_PATH = Path.home() / ".mailbox-cleanup" / "config.json"


def config_path() -> Path:
    """Return the active config path. Honors the env var override."""
    override = os.environ.get(DEFAULT_CONFIG_PATH_ENV)
    return Path(override) if override else DEFAULT_CONFIG_PATH


def _to_dict(cfg: Config) -> dict:
    return {
        "schema_version": cfg.schema_version,
        "default": cfg.default,
        "accounts": [{**asdict(a), "keep": list(a.keep)} for a in cfg.accounts],
    }


def save_config(cfg: Config) -> None:
    """Write config atomically (tmp + rename) with mode 0600 / parent 0700."""
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    tmp = path.with_suffix(path.suffix + ".tmp")
    payload = json.dumps(_to_dict(cfg), ensure_ascii=False, indent=2)
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
    """Read and validate config from the active path.

    Raises FileNotFoundError if the file is missing, ConfigError on parse or
    schema problems.
    """
    path = config_path()
    if not path.exists():
        raise FileNotFoundError(f"Config not found: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ConfigError(f"Could not parse config at {path}: {e}") from e
    return validate_config(data)


class AccountResolutionError(Exception):
    """Raised when no account can be resolved.

    `error_code` is one of: 'no_account_selected', 'unknown_account'.
    """

    def __init__(self, error_code: str, message: str):
        super().__init__(f"{error_code}: {message}")
        self.error_code = error_code


def _find_account(cfg: Config, identifier: str) -> Account | None:
    for a in cfg.accounts:
        if a.alias == identifier or a.email == identifier:
            return a
    return None


def resolve_account(cfg: Config, *, flag: str | None, env: str | None) -> Account:
    """Resolve which Account to operate on.

    Precedence (highest first): flag, env, cfg.default, single-account, hard-fail.
    Empty strings in flag/env are treated as None.
    """
    if flag:
        a = _find_account(cfg, flag)
        if a is None:
            raise AccountResolutionError(
                "unknown_account",
                f"{flag!r}; known: {[x.alias for x in cfg.accounts]}",
            )
        return a
    if env:
        a = _find_account(cfg, env)
        if a is None:
            raise AccountResolutionError(
                "unknown_account",
                f"in MAILBOX_CLEANUP_ACCOUNT={env!r}",
            )
        return a
    if cfg.default:
        a = _find_account(cfg, cfg.default)
        # validate_config guarantees default points at an existing alias; this
        # assertion catches Config instances built directly that bypass it.
        assert a is not None, (
            f"validate_config invariant violated: default {cfg.default!r} not in accounts"
        )
        return a
    if len(cfg.accounts) == 1:
        return cfg.accounts[0]
    raise AccountResolutionError(
        "no_account_selected",
        "Multiple accounts configured. Specify --account=<alias>, "
        "set MAILBOX_CLEANUP_ACCOUNT=, or run "
        "'mailbox-cleanup config set-default <alias>'.",
    )


V01_SERVICE_NAME = "mailbox-cleanup"
V01_SERVER_KEY_PREFIX = "imap-server:"
V01_DEFAULT_SERVER = "imap.ionos.de"


def bootstrap_from_v01_keychain(email: str) -> Config:
    """Create a v0.2 config from a v0.1 single-account Keychain entry.

    Reads the password (must exist) and the optional imap-server entry,
    derives alias and provider, writes config.json, deletes the obsolete
    imap-server key.

    Raises ConfigError if no v0.1 password is in Keychain for the email.
    """
    if keyring.get_password(V01_SERVICE_NAME, email) is None:
        raise ConfigError(f"Bootstrap failed: no v0.1 credentials in Keychain for {email!r}")
    server = (
        keyring.get_password(V01_SERVICE_NAME, f"{V01_SERVER_KEY_PREFIX}{email}")
        or V01_DEFAULT_SERVER
    )
    alias = derive_alias_from_email(email)
    account = Account(alias=alias, email=email, server=server, port=993)
    cfg = Config(default=alias, accounts=(account,))
    save_config(cfg)
    # Clean up obsolete v0.1 server key. keyring backends raise varied exceptions
    # (no entry, locked keychain, backend down). Treat all as best-effort.
    try:
        keyring.delete_password(V01_SERVICE_NAME, f"{V01_SERVER_KEY_PREFIX}{email}")
    except Exception:  # noqa: BLE001 — backend-agnostic best-effort cleanup
        pass
    return cfg
