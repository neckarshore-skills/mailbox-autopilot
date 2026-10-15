"""Shared CLI plumbing: account resolution, auto-bootstrap, --email deprecation."""

from __future__ import annotations

import os
import sys
import warnings
from dataclasses import replace as dc_replace

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
    """User-facing CLI error with structured error_code.

    error_code is one of: 'no_config', 'unknown_account', 'no_account_selected',
    'bootstrap_failed', 'bad_config'.
    """

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
    - --email deprecation (emits DeprecationWarning, treats value as --account)
    - Auto-bootstrap from v0.1 Keychain when no config exists yet but
      --email was provided
    - Account precedence: flag > env > config.default > single account
    """
    # Auto-bootstrap path: no config yet but --email supplied
    if not config_path().exists():
        if email_flag:
            try:
                bootstrap_from_v01_keychain(email_flag)
                print(
                    f"Migrated to multi-account config ({config_path()}). "
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
                f"No config found at {config_path()}. Run "
                "'mailbox-autopilot config init' or pass --account / --email "
                "to bootstrap.",
            )

    # --email deprecation: treat as --account if --account not given
    if email_flag and not account_flag:
        warnings.warn(
            "--email is deprecated; use --account=<alias-or-email>. "
            "It will be removed in a later release.",
            DeprecationWarning,
            stacklevel=2,
        )
        account_flag = email_flag

    try:
        cfg = load_config()
    except ConfigError as e:
        # e.g. a bad keep entry written by hand: refuse before connecting.
        raise AccountFlagsError("bad_config", str(e)) from e
    env_value = os.environ.get("MAILBOX_CLEANUP_ACCOUNT")
    try:
        account = resolve_account(cfg, flag=account_flag, env=env_value)
    except AccountResolutionError as e:
        raise AccountFlagsError(e.error_code, str(e)) from e

    # AuthMissingError propagates to the caller (CLI subcommand emits auth_missing).
    creds = get_credentials(account.email)
    # Override server with account.server: after v0.1 bootstrap the Keychain
    # imap-server:<email> entry is deleted; the config is now the source of truth.
    return account, dc_replace(creds, server=account.server)
