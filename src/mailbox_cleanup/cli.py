import json
import os
import sys
from collections import Counter
from dataclasses import asdict
from dataclasses import replace as dc_replace

import click

from . import SCHEMA_VERSION
from .audit import log_action
from .auth import (
    AuthMissingError,
    delete_credentials,
    set_credentials,
)
from .cli_helpers import AccountFlagsError, resolve_account_and_credentials
from .config import (
    Account,
    AccountResolutionError,
    Config,
    ConfigError,
    bootstrap_from_v01_keychain,
    config_path,
    derive_alias_from_email,
    load_config,
    normalize_keep,
    resolve_account,
    save_config,
)
from .folders import resolve_folder
from .imap_client import imap_connect
from .manage.args import unsafe_arg_keys
from .manage.cli import manage as _manage_group
from .operations.archive import run_archive
from .operations.attachments import run_attachments
from .operations.batching import PartialMoveError, move_in_batches
from .operations.bounces import run_bounces
from .operations.dedupe import run_dedupe
from .operations.delete import PreviewMismatchError, run_delete
from .operations.move import run_move
from .operations.selection import CATEGORIES
from .operations.unsubscribe import (
    EGRESS_REFUSED_PREFIX,
    UnsubAction,
    collect_unsub_targets,
    perform_unsubscribe,
)
from .scan import build_report


def _no_control(ctx, param, value):
    """Click callback: refuse control characters in a value that reaches IMAP.

    imap_tools escapes only `\\` and `"`; a CR/LF would end the IMAP command line and let
    the rest run as a new command (for these commands: a smuggled DELETE or MOVE).
    """
    values = value if isinstance(value, tuple) else (value,)
    if any(v is not None and unsafe_arg_keys(value=v) for v in values):
        raise click.BadParameter("contains a control character (refused before connecting)")
    return value


def _emit(payload: dict, json_mode: bool) -> None:
    if json_mode:
        click.echo(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        for k, v in payload.items():
            click.echo(f"{k}: {v}")


def _fail(payload: dict, exit_code: int, json_mode: bool) -> None:
    payload["ok"] = False
    _emit(payload, json_mode)
    sys.exit(exit_code)


@click.group()
@click.version_option(package_name="mailbox-autopilot")
def cli():
    """Clean up an IMAP mailbox and draft replies. Never sends mail."""


@cli.group()
def auth():
    """Manage mailbox credentials in macOS Keychain."""


@auth.command("set")
@click.option(
    "--alias",
    default=None,
    help="Slug alias for the account (optional; derived from email).",
)
@click.option("--email", required=True, help="Email address.")
@click.option("--server", default="imap.ionos.de", show_default=True)
@click.option("--port", default=993, show_default=True, type=int)
@click.option("--provider", default=None, help="Override auto-derived provider label.")
@click.option("--make-default", is_flag=True, help="Set this account as the default.")
@click.password_option(confirmation_prompt=False, prompt="Password")
def auth_set(alias, email, server, port, provider, make_default, password):
    """Store credentials in Keychain and add the account to the config."""
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
    try:
        account, creds = resolve_account_and_credentials(
            account_flag=account_flag, email_flag=email_flag
        )
    except AccountFlagsError as e:
        _fail(
            {"error_code": e.error_code, "message": str(e)},
            4,
            json_mode,
        )
        return
    except AuthMissingError as e:
        _fail(
            {"error_code": "auth_missing", "message": str(e)},
            3,
            json_mode,
        )
        return

    try:
        with imap_connect(creds, port=account.port) as mb:
            folders = [f.name for f in mb.folder.list()]
    except Exception as e:
        _fail(
            {"error_code": "connection_error", "message": str(e)},
            2,
            json_mode,
        )
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
    try:
        account, _ = resolve_account_and_credentials(
            account_flag=account_flag, email_flag=email_flag
        )
    except AccountFlagsError as e:
        _fail(
            {"error_code": e.error_code, "message": str(e)},
            4,
            json_mode=False,
        )
        return
    except AuthMissingError as e:
        _fail(
            {"error_code": "auth_missing", "message": str(e)},
            3,
            json_mode=False,
        )
        return

    cfg = load_config()
    new_accounts = tuple(a for a in cfg.accounts if a.alias != account.alias)
    new_default = cfg.default if cfg.default != account.alias else None
    save_config(Config(default=new_default, accounts=new_accounts))
    delete_credentials(account.email)
    click.echo(f"Removed account {account.alias} ({account.email}).")


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
    """Create an empty config file (idempotent), or bootstrap from v0.1 Keychain."""
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
        _fail(
            {"error_code": "no_config", "message": f"No config at {config_path()}"},
            5,
            json_mode,
        )
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
        _fail(
            {"error_code": "no_config", "message": f"No config at {config_path()}"},
            5,
            json_mode,
        )
        return
    target = alias or cfg.default
    if target is None:
        _fail(
            {"error_code": "no_account_selected", "message": "No alias given and no default."},
            4,
            json_mode,
        )
        return
    found = next((a for a in cfg.accounts if a.alias == target), None)
    if found is None:
        _fail(
            {"error_code": "unknown_account", "message": f"Unknown alias {target!r}"},
            4,
            json_mode,
        )
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
        _fail(
            {"error_code": "unknown_account", "message": f"No alias {old_alias!r}"},
            4,
            json_mode=False,
        )
        return
    if any(a.alias == new_alias for a in cfg.accounts):
        _fail(
            {"error_code": "duplicate_alias", "message": f"Alias {new_alias!r} already exists"},
            4,
            json_mode=False,
        )
        return
    new_accounts = tuple(
        dc_replace(a, alias=new_alias) if a.alias == old_alias else a for a in cfg.accounts
    )
    new_default = new_alias if cfg.default == old_alias else cfg.default
    save_config(Config(default=new_default, accounts=new_accounts))
    click.echo(f"Renamed {old_alias} -> {new_alias}.")


@config_group.command("remove")
@click.argument("alias")
def config_remove(alias: str):
    """Remove an account from config and delete its Keychain password."""
    cfg = load_config()
    target = next((a for a in cfg.accounts if a.alias == alias), None)
    if target is None:
        _fail(
            {"error_code": "unknown_account", "message": f"No alias {alias!r}"},
            4,
            json_mode=False,
        )
        return
    new_accounts = tuple(a for a in cfg.accounts if a.alias != alias)
    new_default = cfg.default if cfg.default != alias else None
    save_config(Config(default=new_default, accounts=new_accounts))
    delete_credentials(target.email)
    click.echo(f"Removed account {alias} ({target.email}).")


@config_group.group("keep")
def config_keep_group():
    """Senders never acted on in one account: full address or @domain.

    delete, move, unsubscribe and bounces leave mail from these senders alone.
    """


def _keep_target(account_flag, json_mode):
    """Load the config and resolve the account the keep command works on."""
    try:
        cfg = load_config()
    except FileNotFoundError:
        _fail({"error_code": "no_config", "message": f"No config at {config_path()}"}, 5, json_mode)
    except ConfigError as e:
        _fail({"error_code": "bad_config", "message": str(e)}, 4, json_mode)
    try:
        account = resolve_account(
            cfg, flag=account_flag, env=os.environ.get("MAILBOX_CLEANUP_ACCOUNT")
        )
    except AccountResolutionError as e:
        _fail({"error_code": e.error_code, "message": str(e)}, 4, json_mode)
    return cfg, account


def _save_keep(cfg, account, keep):
    accounts = tuple(
        dc_replace(a, keep=keep) if a.alias == account.alias else a for a in cfg.accounts
    )
    save_config(dc_replace(cfg, accounts=accounts))


def _keep_payload(alias, keep):
    return {"account": alias, "keep": list(keep)}


@config_keep_group.command("add")
@click.option("--account", "account_flag", default=None, help="Alias or email.")
@click.argument("entries", nargs=-1, required=True, callback=_no_control)
@click.option("--json", "json_mode", is_flag=True)
def config_keep_add(account_flag, entries, json_mode):
    """Add senders to the keep list of one account."""
    cfg, account = _keep_target(account_flag, json_mode)
    try:
        new = normalize_keep([*account.keep, *entries])
    except ValueError as e:
        _fail({"error_code": "bad_args", "message": str(e)}, 4, json_mode)
    _save_keep(cfg, account, new)
    if json_mode:
        click.echo(json.dumps(_keep_payload(account.alias, new), ensure_ascii=False, indent=2))
    else:
        click.echo(f"Keep list of {account.alias}: {', '.join(new)}")


@config_keep_group.command("remove")
@click.option("--account", "account_flag", default=None, help="Alias or email.")
@click.argument("entries", nargs=-1, required=True, callback=_no_control)
@click.option("--json", "json_mode", is_flag=True)
def config_keep_remove(account_flag, entries, json_mode):
    """Remove senders from the keep list of one account."""
    cfg, account = _keep_target(account_flag, json_mode)
    drop = {(e or "").strip().lower() for e in entries}
    absent = sorted(drop - set(account.keep))
    if absent:
        _fail(
            {
                "error_code": "not_in_keep_list",
                "message": f"Not on the keep list of {account.alias}: {', '.join(absent)}",
            },
            4,
            json_mode,
        )
    new = tuple(e for e in account.keep if e not in drop)
    _save_keep(cfg, account, new)
    if json_mode:
        click.echo(json.dumps(_keep_payload(account.alias, new), ensure_ascii=False, indent=2))
    else:
        click.echo(f"Keep list of {account.alias}: {', '.join(new) or '(empty)'}")


@config_keep_group.command("list")
@click.option("--account", "account_flag", default=None, help="Alias or email.")
@click.option("--json", "json_mode", is_flag=True)
def config_keep_list(account_flag, json_mode):
    """Show the keep list of one account."""
    _cfg, account = _keep_target(account_flag, json_mode)
    if json_mode:
        click.echo(
            json.dumps(_keep_payload(account.alias, account.keep), ensure_ascii=False, indent=2)
        )
    else:
        click.echo(f"Keep list of {account.alias}: {', '.join(account.keep) or '(empty)'}")


@cli.command("scan")
@click.option("--account", "account_flag", default=None, help="Alias or email.")
@click.option("--email", "email_flag", default=None, help="Deprecated; use --account.")
@click.option("--folder", default="INBOX", show_default=True, callback=_no_control)
@click.option("--json", "json_mode", is_flag=True)
def scan_cmd(account_flag, email_flag, folder: str, json_mode: bool):
    """Scan a folder, classify messages, emit a discovery report."""
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
        with imap_connect(creds, port=account.port) as mb:
            mb.folder.set(folder)
            messages = list(mb.fetch(headers_only=True, mark_seen=False, bulk=True))
    except Exception as e:
        _fail({"error_code": "connection_error", "message": str(e)}, 2, json_mode)
        return
    report = build_report(messages, folder=folder)
    if json_mode:
        click.echo(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        click.echo(f"Folder: {report['folder']}")
        click.echo(f"Total: {report['total_messages']} messages, {report['size_total_mb']} MB")
        for name, data in report["categories"].items():
            count = data.get("count") if isinstance(data, dict) else None
            if count is not None:
                click.echo(f"  {name}: {count}")


@cli.command("senders")
@click.option("--account", "account_flag", default=None, help="Alias or email.")
@click.option("--email", "email_flag", default=None, help="Deprecated; use --account.")
@click.option("--folder", default="INBOX", show_default=True, callback=_no_control)
@click.option("--top", default=50, show_default=True, type=int)
@click.option("--json", "json_mode", is_flag=True)
def senders_cmd(account_flag, email_flag, folder: str, top: int, json_mode: bool):
    """List the top-N senders by message count."""
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
        with imap_connect(creds, port=account.port) as mb:
            mb.folder.set(folder)
            counter: Counter[str] = Counter()
            for m in mb.fetch(headers_only=True, mark_seen=False, bulk=True):
                if m.from_:
                    counter[m.from_.strip()] += 1
    except Exception as e:
        _fail({"error_code": "connection_error", "message": str(e)}, 2, json_mode)
        return
    payload = {
        "schema_version": SCHEMA_VERSION,
        "folder": folder,
        "senders": [{"sender": s, "count": c} for s, c in counter.most_common(top)],
    }
    if json_mode:
        click.echo(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        for entry in payload["senders"]:
            click.echo(f"{entry['count']:6d}  {entry['sender']}")


def _require_filter(sender, subject_contains, older_than, json_mode, recipient=None, category=None):
    if not any([sender, subject_contains, older_than, recipient, category]):
        _fail(
            {
                "error_code": "bad_args",
                "message": (
                    "At least one filter required (--sender / --subject-contains / "
                    "--older-than / --recipient / --category)"
                ),
            },
            4,
            json_mode,
        )


def _selection_options(f):
    """Filters shared by delete and move (#50). Applied bottom-up by click."""
    f = click.option(
        "--expect-count",
        type=int,
        default=None,
        help="With --apply: the affected_count the user confirmed. Refuses if it changed.",
    )(f)
    f = click.option("--limit", default=None, type=int)(f)
    f = click.option(
        "--keep",
        multiple=True,
        callback=_no_control,
        help="Never act on this sender: full address or @domain (subdomains included). Repeatable.",
    )(f)
    f = click.option(
        "--category",
        type=click.Choice(sorted(CATEGORIES)),
        default=None,
        help="Only messages the classifier puts in this category.",
    )(f)
    f = click.option("--older-than", default=None, help="e.g. 30d, 2w, 3m, 1y")(f)
    f = click.option("--recipient", default=None, callback=_no_control, help="To: address.")(f)
    f = click.option("--subject-contains", default=None, callback=_no_control)(f)
    f = click.option(
        "--sender", multiple=True, callback=_no_control, help="Repeatable; any of them matches."
    )(f)
    return f


def _selection_args(sender, subject_contains, older_than, recipient, category, keep, limit):
    return {
        "sender": list(sender),
        "subject_contains": subject_contains,
        "older_than": older_than,
        "recipient": recipient,
        "category": category,
        "keep": list(keep),
        "limit": limit,
    }


def _run_selection_action(*, subcommand, account, json_mode, apply, folder, args, run, text_target):
    """Shared body of delete and move: run, map errors to exit codes, audit, emit."""
    # The audit log has to explain why a mail survived: record the account's list too.
    args = {**args, "keep_config": list(account[0].keep)}
    try:
        with imap_connect(account[1], port=account[0].port) as mb:
            res = run(mb)
    except PreviewMismatchError as e:
        _fail(
            {
                "error_code": "preview_mismatch",
                "message": str(e),
                "expected": e.expected,
                "found": e.found,
            },
            4,
            json_mode,
        )
        return
    except PartialMoveError as e:
        log_action(
            subcommand=subcommand,
            account=account[0].alias,
            args=args,
            folder=folder,
            affected_uids=e.moved,
            result="partial_failure",
            error=type(e.cause).__name__,
        )
        _fail(
            {
                "error_code": "partial_failure",
                "message": str(e),
                "moved_count": len(e.moved),
                "not_moved_count": len(e.remaining),
            },
            5,
            json_mode,
        )
        return
    except ValueError as e:
        _fail({"error_code": "bad_args", "message": str(e)}, 4, json_mode)
        return
    except Exception as e:
        _fail({"error_code": "operation_error", "message": str(e)}, 2, json_mode)
        return
    payload = {
        "ok": True,
        "schema_version": SCHEMA_VERSION,
        "subcommand": subcommand,
        "dry_run": res.dry_run,
        "folder": res.folder,
        "target_folder": res.target_folder,
        "affected_count": len(res.affected_uids),
        "affected_uids": res.affected_uids,
        "kept_count": res.kept_count,
        "kept_by_config": res.kept_by_config,
        "keep_config": list(account[0].keep),
        "by_sender": res.by_sender,
        "sample": res.sample,
    }
    if apply:
        log_action(
            subcommand=subcommand,
            account=account[0].alias,
            args=args,
            folder=folder,
            affected_uids=res.affected_uids,
            result="success",
        )
    if json_mode:
        click.echo(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        verb = "Moved" if apply else "Would move"
        click.echo(f"{verb} {len(res.affected_uids)} messages to {text_target(res)!r}")


def _resolve(account_flag, email_flag, json_mode):
    try:
        return resolve_account_and_credentials(account_flag=account_flag, email_flag=email_flag)
    except AccountFlagsError as e:
        _fail({"error_code": e.error_code, "message": str(e)}, 4, json_mode)
    except AuthMissingError as e:
        _fail({"error_code": "auth_missing", "message": str(e)}, 3, json_mode)
    return None


@cli.command("delete")
@click.option("--account", "account_flag", default=None, help="Alias or email.")
@click.option("--email", "email_flag", default=None, help="Deprecated; use --account.")
@click.option("--folder", default="INBOX", show_default=True, callback=_no_control)
@_selection_options
@click.option(
    "--apply",
    is_flag=True,
    help="Actually move to Trash. Without --apply this is a dry-run.",
)
@click.option("--json", "json_mode", is_flag=True)
def delete_cmd(
    account_flag,
    email_flag,
    folder,
    sender,
    subject_contains,
    recipient,
    older_than,
    category,
    keep,
    limit,
    expect_count,
    apply,
    json_mode,
):
    """Soft-delete messages matching filter (move to Trash)."""
    _require_filter(sender, subject_contains, older_than, json_mode, recipient, category)
    resolved = _resolve(account_flag, email_flag, json_mode)
    if resolved is None:
        return
    _run_selection_action(
        subcommand="delete",
        account=resolved,
        json_mode=json_mode,
        apply=apply,
        folder=folder,
        args=_selection_args(
            sender, subject_contains, older_than, recipient, category, keep, limit
        ),
        run=lambda mb: run_delete(
            mb,
            folder=folder,
            sender=sender,
            subject_contains=subject_contains,
            older_than=older_than,
            recipient=recipient,
            category=category,
            keep=keep,
            config_keep=resolved[0].keep,
            apply=apply,
            limit=limit,
            expect_count=expect_count,
        ),
        text_target=lambda res: res.target_folder,
    )


@cli.command("move")
@click.option("--account", "account_flag", default=None, help="Alias or email.")
@click.option("--email", "email_flag", default=None, help="Deprecated; use --account.")
@click.option("--folder", default="INBOX", show_default=True, callback=_no_control)
@click.option("--to", "target", required=True, help="Destination folder.", callback=_no_control)
@_selection_options
@click.option("--apply", is_flag=True)
@click.option("--json", "json_mode", is_flag=True)
def move_cmd(
    account_flag,
    email_flag,
    folder,
    target,
    sender,
    subject_contains,
    recipient,
    older_than,
    category,
    keep,
    limit,
    expect_count,
    apply,
    json_mode,
):
    """Move messages matching filter to target folder."""
    _require_filter(sender, subject_contains, older_than, json_mode, recipient, category)
    resolved = _resolve(account_flag, email_flag, json_mode)
    if resolved is None:
        return
    args = _selection_args(sender, subject_contains, older_than, recipient, category, keep, limit)
    _run_selection_action(
        subcommand="move",
        account=resolved,
        json_mode=json_mode,
        apply=apply,
        folder=folder,
        args={"to": target, **args},
        run=lambda mb: run_move(
            mb,
            folder=folder,
            target=target,
            sender=sender,
            subject_contains=subject_contains,
            older_than=older_than,
            recipient=recipient,
            category=category,
            keep=keep,
            config_keep=resolved[0].keep,
            apply=apply,
            limit=limit,
            expect_count=expect_count,
        ),
        text_target=lambda res: res.target_folder,
    )


@cli.command("archive")
@click.option("--account", "account_flag", default=None, help="Alias or email.")
@click.option("--email", "email_flag", default=None, help="Deprecated; use --account.")
@click.option("--folder", default="INBOX", show_default=True, callback=_no_control)
@click.option("--older-than", required=True, help="e.g. 12m, 2y")
@click.option("--apply", is_flag=True)
@click.option("--json", "json_mode", is_flag=True)
def archive_cmd(account_flag, email_flag, folder, older_than, apply, json_mode):
    """Bulk-move messages older than N into Archive/YYYY subfolders."""
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
        with imap_connect(creds, port=account.port) as mb:
            res = run_archive(mb, folder=folder, older_than=older_than, apply=apply)
    except Exception as e:
        _fail({"error_code": "operation_error", "message": str(e)}, 2, json_mode)
        return
    affected = sum(g["count"] for g in res.groups)
    payload = {
        "ok": True,
        "schema_version": SCHEMA_VERSION,
        "subcommand": "archive",
        "dry_run": res.dry_run,
        "folder": res.folder,
        "archive_root": res.archive_root,
        "groups": res.groups,
        "affected_count": affected,
    }
    if apply:
        log_action(
            subcommand="archive",
            account=account.alias,
            args={"older_than": older_than},
            folder=folder,
            affected_uids=[uid for g in res.groups for uid in g["uids"]],
            result="success",
        )
    if json_mode:
        click.echo(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        verb = "Moved" if apply else "Would move"
        click.echo(f"{verb} {affected} messages into {res.archive_root}/<year>")
        for g in res.groups:
            click.echo(f"  {g['year']}: {g['count']} → {g['target']}")


@cli.command("dedupe")
@click.option("--account", "account_flag", default=None, help="Alias or email.")
@click.option("--email", "email_flag", default=None, help="Deprecated; use --account.")
@click.option("--folder", default="INBOX", show_default=True, callback=_no_control)
@click.option("--apply", is_flag=True)
@click.option("--json", "json_mode", is_flag=True)
def dedupe_cmd(account_flag, email_flag, folder, apply, json_mode):
    """Move duplicate-by-Message-ID copies to Trash, keep the oldest."""
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
        with imap_connect(creds, port=account.port) as mb:
            res = run_dedupe(mb, folder=folder, apply=apply)
    except Exception as e:
        _fail({"error_code": "operation_error", "message": str(e)}, 2, json_mode)
        return
    payload = {
        "ok": True,
        "schema_version": SCHEMA_VERSION,
        "subcommand": "dedupe",
        "dry_run": res.dry_run,
        "folder": res.folder,
        "target_folder": res.target_folder,
        "duplicate_count": len(res.duplicate_uids),
        "groups": res.groups,
    }
    if apply:
        log_action(
            subcommand="dedupe",
            account=account.alias,
            args={},
            folder=folder,
            affected_uids=res.duplicate_uids,
            result="success",
        )
    if json_mode:
        click.echo(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        verb = "Moved" if apply else "Would move"
        click.echo(
            f"{verb} {len(res.duplicate_uids)} duplicates from {len(res.groups)} groups to Trash"
        )


@cli.command("attachments")
@click.option("--account", "account_flag", default=None, help="Alias or email.")
@click.option("--email", "email_flag", default=None, help="Deprecated; use --account.")
@click.option("--folder", default="INBOX", show_default=True, callback=_no_control)
@click.option("--size-gt", default="10mb", show_default=True)
@click.option("--older-than", default=None)
@click.option("--json", "json_mode", is_flag=True)
def attachments_cmd(account_flag, email_flag, folder, size_gt, older_than, json_mode):
    """Find large messages. Stripping deferred to v2 — this lists candidates only."""
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
        with imap_connect(creds, port=account.port) as mb:
            res = run_attachments(mb, folder=folder, size_gt=size_gt, older_than=older_than)
    except Exception as e:
        _fail({"error_code": "operation_error", "message": str(e)}, 2, json_mode)
        return
    payload = {
        "ok": True,
        "schema_version": SCHEMA_VERSION,
        "subcommand": "attachments",
        "dry_run": res.dry_run,
        "folder": res.folder,
        "candidate_count": len(res.candidates),
        "candidates": res.candidates,
        "note": (
            "v1 lists candidates only; "
            "pipe to `delete --sender=... --apply` to remove specific senders."
        ),
    }
    if json_mode:
        click.echo(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        click.echo(f"Found {len(res.candidates)} large messages:")
        for c in res.candidates[:20]:
            click.echo(f"  {c['size_mb']:>6.1f} MB  {c['from']}  {c['subject']}")


@cli.command("unsubscribe")
@click.option("--account", "account_flag", default=None, help="Alias or email.")
@click.option("--email", "email_flag", default=None, help="Deprecated; use --account.")
@click.option("--folder", default="INBOX", show_default=True, callback=_no_control)
@click.option("--sender", required=True, callback=_no_control)
@click.option("--apply", is_flag=True)
@click.option("--json", "json_mode", is_flag=True)
def unsubscribe_cmd(account_flag, email_flag, folder, sender, apply, json_mode):
    """Parse List-Unsubscribe for sender; optionally run HTTPS one-click.

    mailto-only senders are listed under manual_unsubscribe and their mail is kept.
    A failed HTTPS unsubscribe keeps the mail too, reports ok=false and exits 5
    (unsubscribe_failed): the failed link and any mailto alternative are listed
    under manual_unsubscribe. A link the egress guard refused is never listed for
    manual handling; it goes to refused_unsubscribe instead.
    """
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
    uids: list[str] = []
    actions: list[dict] = []
    results: list[dict] = []
    manual: list[str] = []
    refused: list[str] = []
    kept_count = 0
    failed = False
    try:
        with imap_connect(creds, port=account.port) as mb:
            data = collect_unsub_targets(mb, sender=sender, folder=folder, keep=account.keep)
            uids = data["uids"]
            actions = data["actions"]
            kept_count = data.get("kept_count", 0)
            https_actions = [a for a in actions if a["kind"] == "https"]
            if not https_actions:
                manual = [a["target"] for a in actions if a["kind"] == "mailto"]
            if apply:
                if https_actions:
                    first = https_actions[0]
                    a = UnsubAction(**{k: first[k] for k in ("kind", "target", "one_click")})
                    ok, info = perform_unsubscribe(a)
                    results.append({"action": first, "ok": ok, "info": info})
                    if not ok:
                        # Founder 2026-10-08: a failed unsubscribe keeps the mail and
                        # lists the target for manual handling.
                        failed = True
                        mailto = [a["target"] for a in actions if a["kind"] == "mailto"]
                        if info.startswith(EGRESS_REFUSED_PREFIX):
                            # Never hand a person a link the tool refused as unsafe.
                            refused = [first["target"]]
                            manual = mailto
                        else:
                            manual = [first["target"], *mailto]
                # HTTPS success or no header: move to Trash. mailto-only or failed: keep.
                if not manual and not failed:
                    trash = resolve_folder(mb, "trash")
                    if trash and uids:
                        move_in_batches(mb, uids, trash)
    except Exception as e:
        _fail({"error_code": "operation_error", "message": str(e)}, 2, json_mode)
        return
    payload = {
        "ok": True,
        "schema_version": SCHEMA_VERSION,
        "subcommand": "unsubscribe",
        "dry_run": not apply,
        "folder": folder,
        "sender": sender,
        "matching_count": len(uids),
        "kept_count": kept_count,
        "actions": actions,
        "results": results,
        "manual_unsubscribe": manual,
        "refused_unsubscribe": refused,
    }
    if failed:
        payload["ok"] = False
        payload["error_code"] = "unsubscribe_failed"
    if apply:
        if failed:
            result = "failed"
        elif manual:
            result = "manual"
        else:
            result = "success"
        log_action(
            subcommand="unsubscribe",
            account=account.alias,
            args={"sender": sender},
            folder=folder,
            affected_uids=[] if (manual or failed) else uids,
            result=result,
        )
    if json_mode:
        click.echo(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        counts = f"{len(actions)} action(s) found, {len(uids)} matching messages"
        if kept_count:
            counts += f", {kept_count} kept by the keep list"
        if apply and manual and not failed:
            click.echo(f"No unsubscribe performed for {sender}, all mail kept: {counts}")
        elif apply and failed:
            click.echo(
                f"Unsubscribe FAILED for {sender} ({results[0]['info']}), all mail kept: {counts}"
            )
        elif manual:
            click.echo(f"No automatic unsubscribe available for {sender}: {counts}")
        else:
            verb = "Performed" if apply else "Would attempt"
            click.echo(f"{verb} unsubscribe for {sender}: {counts}")
        if manual:
            label = "Unsubscribe by hand" if failed else "Unsubscribe by hand (mailto only)"
            click.echo(f"{label}: {', '.join(manual)}")
        if refused:
            click.echo(f"Refused as unsafe, do not open: {', '.join(refused)}")
    if failed:
        sys.exit(5)


@cli.command("bounces")
@click.option("--account", "account_flag", default=None, help="Alias or email.")
@click.option("--email", "email_flag", default=None, help="Deprecated; use --account.")
@click.option("--folder", default="INBOX", show_default=True, callback=_no_control)
@click.option("--apply", is_flag=True)
@click.option("--json", "json_mode", is_flag=True)
def bounces_cmd(account_flag, email_flag, folder, apply, json_mode):
    """Find bounce/auto-reply messages, optionally move to Trash."""
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
        with imap_connect(creds, port=account.port) as mb:
            res = run_bounces(mb, folder=folder, apply=apply, keep=account.keep)
    except Exception as e:
        _fail({"error_code": "operation_error", "message": str(e)}, 2, json_mode)
        return
    payload = {
        "ok": True,
        "schema_version": SCHEMA_VERSION,
        "subcommand": "bounces",
        "dry_run": res.dry_run,
        "folder": res.folder,
        "target_folder": res.target_folder,
        "affected_count": len(res.affected_uids),
        "affected_uids": res.affected_uids,
        "kept_count": res.kept_count,
        "sample": res.sample,
    }
    if apply:
        log_action(
            subcommand="bounces",
            account=account.alias,
            args={"keep_config": list(account.keep)},
            folder=folder,
            affected_uids=res.affected_uids,
            result="success",
        )
    if json_mode:
        click.echo(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        verb = "Moved" if apply else "Would move"
        click.echo(f"{verb} {len(res.affected_uids)} bounce/auto-reply messages to Trash")


cli.add_command(_manage_group)
