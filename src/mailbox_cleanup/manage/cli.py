"""`manage` subcommands (spec §3 layer 3). Read and draft only."""

from __future__ import annotations

import datetime
import json
import sys
from collections.abc import Iterable

import click
from click.core import ParameterSource

from .. import SCHEMA_VERSION
from ..audit import log_manage_action
from ..auth import AuthMissingError
from ..cli_helpers import AccountFlagsError, resolve_account_and_credentials
from ..config import Account
from ..imap_client import imap_connect
from . import bodyfile
from .args import unsafe_arg_keys
from .attachments import (
    DestinationError,
    NoSuchAttachmentError,
    fetch_attachment,
    resolve_destination,
    write_exclusive,
)
from .compose import ComposeError, build_new
from .draft import NoDraftsFolderError, RecipientError, build_reply, plan_recipients, save_draft
from .envelope import wrap
from .headers import is_bare_address
from .ids import safe_message_id
from .playbooks import load_playbooks, public_playbooks
from .read import _UID_RE, Message, read_message
from .search import search
from .sources import SourcesConfigError, load_sources
from .thread import thread


def _out(payload: dict) -> None:
    payload.setdefault("schema_version", SCHEMA_VERSION)
    click.echo(json.dumps(payload, ensure_ascii=False, indent=2))


def _fail(code: str, message: str, exit_code: int) -> None:
    _out({"ok": False, "error_code": code, "message": message})
    sys.exit(exit_code)


def _fail_audited(
    *,
    subcommand: str,
    account: Account,
    folder: str,
    arg_keys: Iterable[str],
    code: str,
    message: str,
    exit_code: int,
) -> None:
    """Fail after the account is resolved: the audit record carries the error CODE only,
    never the message, which can echo search values or mail content."""
    log_manage_action(
        subcommand=subcommand,
        account=account.alias,
        folder=folder,
        uids=[],
        result="error",
        arg_keys=arg_keys,
        error=code,
    )
    _fail(code, message, exit_code)


def _resolve(account_flag):
    """Resolution failures are not audited: no account is known and no mailbox is touched."""
    try:
        return resolve_account_and_credentials(account_flag=account_flag, email_flag=None)
    except AccountFlagsError as e:
        _fail(e.error_code, str(e), 4)
    except AuthMissingError as e:
        _fail("auth_missing", str(e), 3)


def _reject_control_chars(fail: dict, **values: str | None) -> None:
    """Audited `bad_args` before any IMAP call when a value carries a control character.
    The message names the fields, never the values."""
    bad = unsafe_arg_keys(**values)
    if bad:
        fields = ", ".join(f"--{k}" for k in bad)
        _fail_audited(
            **{**fail, "folder": "" if "folder" in bad else fail["folder"]},
            code="bad_args",
            message=f"control characters are not allowed in {fields}",
            exit_code=4,
        )


def _arg_keys(**given) -> list[str]:
    return [k for k, v in given.items() if v]


@click.group("manage")
def manage():
    """Search, read, follow threads, draft replies and new mail. Never sends, deletes or moves."""


@manage.command("search")
@click.option("--account", "account_flag", default=None)
@click.option("--folder", default="INBOX", show_default=True)
@click.option("--sender", default=None)
@click.option("--recipient", default=None, help="Matches the To header only.")
@click.option("--subject", default=None)
@click.option("--text", default=None)
@click.option("--since", default=None, help="YYYY-MM-DD, compared with the Date header")
@click.option("--limit", default=20, show_default=True, type=click.IntRange(min=1))
@click.option("--json", "json_mode", is_flag=True, help="Accepted for symmetry; output is JSON.")
def search_cmd(account_flag, folder, sender, recipient, subject, text, since, limit, json_mode):
    account, creds = _resolve(account_flag)
    keys = _arg_keys(sender=sender, recipient=recipient, subject=subject, text=text, since=since)
    fail = dict(subcommand="manage.search", account=account, folder=folder, arg_keys=keys)
    _reject_control_chars(
        fail, folder=folder, sender=sender, recipient=recipient, subject=subject, text=text
    )
    try:
        since_d = datetime.date.fromisoformat(since) if since else None
    except ValueError:
        _fail_audited(**fail, code="bad_args", message="--since must be YYYY-MM-DD", exit_code=4)
    try:
        with imap_connect(creds, port=account.port) as mb:
            hits = search(
                mb,
                folder=folder,
                sender=sender,
                recipient=recipient,
                subject=subject,
                text=text,
                since=since_d,
                limit=limit,
            )
    except Exception as e:  # surfaced as a structured error; the audit keeps the code only
        # Never str(e): server text can echo search values or mail content.
        _fail_audited(
            **fail,
            code="operation_error",
            message=f"IMAP operation failed ({type(e).__name__})",
            exit_code=2,
        )
    log_manage_action(
        subcommand="manage.search",
        account=account.alias,
        folder=folder,
        uids=[h.uid for h in hits],
        result="success",
        arg_keys=keys,
    )
    _out(
        {
            "ok": True,
            "subcommand": "manage.search",
            "folder": folder,
            "candidates": [
                {
                    "uid": h.uid,
                    "date": h.date,
                    # To is shown so a Sent-folder hit names whom the owner wrote to; like
                    # From and Subject it is mail content and stays inside the envelope.
                    "mail": wrap(f"From: {h.sender}\nTo: {h.to}\nSubject: {h.subject}"),
                }
                for h in hits
            ],
        }
    )


# `message_id` sits outside the envelope alongside `uid`/`folder` because Task 7 threads
# replies off it, but unlike those it is mail-derived (R9/M3): it comes straight off the
# Message-ID header. `_safe_message_id` is `ids.safe_message_id` (Task 7 dispatch ruling
# R1: one shared definition, reused here and by draft.py, not copied) — validated against
# the SAME strict allowlist used before a Message-ID reaches IMAP as a search value, not
# the looser extraction shape `read._MSGID_RE`, plus a length cap. A value that fails
# either check becomes an empty string instead of leaking whatever a hostile mail put
# there.
_safe_message_id = safe_message_id


def _message_json(m: Message) -> dict:
    head = f"From: {m.sender}\nTo: {', '.join(m.to)}\nSubject: {m.subject}\nDate: {m.date}"
    return {
        "uid": m.uid,
        "folder": m.folder,
        "message_id": _safe_message_id(m.message_id),
        "mail": wrap(f"{head}\n\n{m.text}"),
        # index and size are ours and sit outside the envelope. The media type and the file
        # name are whatever the sender chose, so both are enveloped like every mail string.
        "attachments": [
            {
                "index": a.index,
                "size_bytes": a.size,
                "content_type": wrap(a.content_type),
                "filename": wrap(a.filename),
            }
            for a in m.attachments
        ],
    }


@manage.command("read")
@click.option("--account", "account_flag", default=None)
@click.option("--folder", default="INBOX", show_default=True)
@click.option("--uid", required=True)
@click.option("--json", "json_mode", is_flag=True, help="Accepted for symmetry; output is JSON.")
def read_cmd(account_flag, folder, uid, json_mode):
    account, creds = _resolve(account_flag)
    fail = dict(subcommand="manage.read", account=account, folder=folder, arg_keys=["uid"])
    _reject_control_chars(fail, folder=folder)
    if not _UID_RE.fullmatch(uid):
        _fail_audited(
            **fail, code="bad_args", message="--uid must contain only ASCII digits", exit_code=4
        )
    try:
        with imap_connect(creds, port=account.port) as mb:
            m = read_message(mb, uid=uid, folder=folder)
    except Exception as e:  # never str(e): server text can echo mail content
        _fail_audited(
            **fail,
            code="operation_error",
            message=f"IMAP operation failed ({type(e).__name__})",
            exit_code=2,
        )
    if m is None:
        _fail_audited(
            **fail, code="not_found", message=f"no message with UID {uid} in {folder}", exit_code=1
        )
    log_manage_action(
        subcommand="manage.read",
        account=account.alias,
        folder=folder,
        uids=[uid],
        result="success",
        arg_keys=["uid"],
    )
    _out({"ok": True, "subcommand": "manage.read", "message": _message_json(m)})


@manage.command("save-attachment")
@click.option("--account", "account_flag", default=None)
@click.option("--folder", default="INBOX", show_default=True)
@click.option("--uid", required=True)
@click.option("--index", required=True, type=click.IntRange(min=1), help="From `manage read`.")
@click.option("--out", required=True, help="File to create. Never overwrites.")
@click.option("--json", "json_mode", is_flag=True, help="Accepted for symmetry; output is JSON.")
def save_attachment_cmd(account_flag, folder, uid, index, out, json_mode):
    """Write one attachment to --out. Changes nothing in the mailbox."""
    account, creds = _resolve(account_flag)
    sub = "manage.save-attachment"
    keys = ["index", "out", "uid"]
    fail = dict(subcommand=sub, account=account, folder=folder, arg_keys=keys)
    _reject_control_chars(fail, folder=folder, out=out)
    if not _UID_RE.fullmatch(uid):
        _fail_audited(
            **fail, code="bad_args", message="--uid must contain only ASCII digits", exit_code=4
        )
    # The destination is checked before the mailbox is touched: a refused path costs no
    # IMAP call, and the message names the rule, never the path.
    try:
        target = resolve_destination(out)
    except DestinationError as e:
        _fail_audited(**fail, code="bad_args", message=str(e), exit_code=4)
    try:
        with imap_connect(creds, port=account.port) as mb:
            found = fetch_attachment(mb, uid=uid, index=index, folder=folder)
    except NoSuchAttachmentError:
        _fail_audited(
            **fail,
            code="no_such_attachment",
            message=f"message {uid} has no attachment {index}",
            exit_code=1,
        )
    except Exception as e:  # never str(e): server text can echo mail content
        _fail_audited(
            **fail,
            code="operation_error",
            message=f"IMAP operation failed ({type(e).__name__})",
            exit_code=2,
        )
    if found is None:
        _fail_audited(
            **fail, code="not_found", message=f"no message with UID {uid} in {folder}", exit_code=1
        )
    att, payload = found
    try:
        digest = write_exclusive(target, payload)
    except OSError as e:  # appeared since the check, or not writable
        _fail_audited(
            **fail,
            code="write_failed",
            message=f"could not create --out ({type(e).__name__}); nothing was overwritten",
            exit_code=4,
        )
    log_manage_action(
        subcommand=sub,
        account=account.alias,
        folder=folder,
        uids=[uid],
        result="success",
        arg_keys=keys,
    )
    _out(
        {
            "ok": True,
            "subcommand": sub,
            "uid": uid,
            "folder": folder,
            "index": att.index,
            "path": str(target),
            "size_bytes": len(payload),
            "sha256": digest,
            "content_type": wrap(att.content_type),
            "filename": wrap(att.filename),
        }
    )


@manage.command("thread")
@click.option("--account", "account_flag", default=None)
@click.option("--folder", default="INBOX", show_default=True)
@click.option("--uid", required=True)
@click.option("--json", "json_mode", is_flag=True, help="Accepted for symmetry; output is JSON.")
def thread_cmd(account_flag, folder, uid, json_mode):
    account, creds = _resolve(account_flag)
    fail = dict(subcommand="manage.thread", account=account, folder=folder, arg_keys=["uid"])
    _reject_control_chars(fail, folder=folder)
    if not _UID_RE.fullmatch(uid):
        _fail_audited(
            **fail, code="bad_args", message="--uid must contain only ASCII digits", exit_code=4
        )
    try:
        with imap_connect(creds, port=account.port) as mb:
            msgs = thread(mb, uid=uid, folder=folder)
    except Exception as e:  # never str(e): server text can echo mail content
        _fail_audited(
            **fail,
            code="operation_error",
            message=f"IMAP operation failed ({type(e).__name__})",
            exit_code=2,
        )
    if not msgs:
        # thread() returns [] ONLY when the start UID does not exist (read_message finds
        # nothing): whenever a start message IS found, it is always included in the
        # result, so an empty list is an unambiguous not_found signal here (M5).
        _fail_audited(
            **fail, code="not_found", message=f"no message with UID {uid} in {folder}", exit_code=1
        )
    log_manage_action(
        subcommand="manage.thread",
        account=account.alias,
        folder=folder,
        uids=[m.uid for m in msgs],
        result="success",
        arg_keys=["uid"],
    )
    _out(
        {
            "ok": True,
            "subcommand": "manage.thread",
            "messages": [_message_json(m) for m in msgs],
        }
    )


_BODY_FILE_MESSAGE = (
    "--body-file must be a regular UTF-8 text file of at most 1 MB that lies directly in "
    "the outbox folder; `manage outbox` creates the folder and prints its path"
)


@manage.command("outbox")
@click.option("--json", "json_mode", is_flag=True, help="Accepted for symmetry; output is JSON.")
def outbox_cmd(json_mode):
    """Create the outbox folder if it is missing and print its path. `manage draft` and
    `manage compose` read their text from a file in this folder only. Touches no account
    and no mailbox, so it is not audited."""
    path = bodyfile.ensure_outbox()
    if path is None:
        _fail(
            "outbox_unusable",
            "the outbox folder must be a real directory that only you can read "
            f"(mode 700): {bodyfile.outbox_dir()}",
            4,
        )
    _out(
        {
            "ok": True,
            "subcommand": "manage.outbox",
            "path": path,
            "max_bytes": bodyfile.MAX_BODY_BYTES,
        }
    )


@manage.command("draft")
@click.option("--account", "account_flag", default=None)
@click.option("--folder", default="INBOX", show_default=True)
@click.option("--uid", required=True, help="UID of the mail being answered")
@click.option("--body-file", default=None, help="A text file in the outbox; see `manage outbox`.")
@click.option(
    "--preview", is_flag=True, help="Show the planned recipients and subject; write nothing."
)
@click.option("--sender-only", is_flag=True, help="Answer the sender only, not everyone.")
@click.option(
    "--drop",
    multiple=True,
    help="Leave one planned recipient out; repeat for more. Cannot add anyone.",
)
@click.option("--json", "json_mode", is_flag=True, help="Accepted for symmetry; output is JSON.")
def draft_cmd(account_flag, folder, uid, body_file, preview, sender_only, drop, json_mode):
    """Put a reply into the Drafts folder. Answers everyone by default. Never sends."""
    account, creds = _resolve(account_flag)
    arg_keys = ["uid"] + (["body_file"] if body_file is not None else [])
    # R4: "folder" joins arg_keys only when the user actually passed --folder, not for
    # its default — unlike search/read/thread, which never track it at all.
    ctx = click.get_current_context()
    if ctx.get_parameter_source("folder") == ParameterSource.COMMANDLINE:
        arg_keys.append("folder")
    arg_keys += _arg_keys(preview=preview, sender_only=sender_only, drop=drop)
    fail = dict(subcommand="manage.draft", account=account, folder=folder, arg_keys=arg_keys)
    _reject_control_chars(fail, folder=folder)
    if not _UID_RE.fullmatch(uid):
        _fail_audited(
            **fail, code="bad_args", message="--uid must contain only ASCII digits", exit_code=4
        )
    if not all(is_bare_address(d) for d in drop):
        _fail_audited(
            **fail,
            code="bad_args",
            message="--drop takes one bare ASCII address per use (name@example.org)",
            exit_code=4,
        )
    body = None
    if not preview:
        if body_file is None:
            _fail_audited(
                **fail,
                code="bad_args",
                message="--body-file is required unless --preview is given",
                exit_code=4,
            )
        # --body-file is a plain str, not click.Path(...): click would reject a missing file
        # or a directory itself (exit 2, usage text, no JSON, no audit). Every refusal of
        # the file ends in the same audited bad_args; see manage/bodyfile.py for the rules.
        body = bodyfile.read_body_file(body_file)
        if body is None:
            _fail_audited(**fail, code="bad_args", message=_BODY_FILE_MESSAGE, exit_code=4)
    not_found = False
    refused = None
    drafts = None
    try:
        with imap_connect(creds, port=account.port) as mb:
            orig = read_message(mb, uid=uid, folder=folder)
            if orig is None:
                not_found = True
            else:
                try:
                    plan = plan_recipients(
                        orig, own_addr=account.email, sender_only=sender_only, drop=drop
                    )
                except RecipientError as e:
                    refused = e
                else:
                    msg, warnings = build_reply(
                        orig, from_addr=account.email, body=body or "", recipients=plan
                    )
                    if not preview:
                        drafts = save_draft(mb, msg)
    except NoDraftsFolderError:  # Minor 1: never str(e) — a literal message, per R4
        _fail_audited(
            **fail, code="no_drafts_folder", message="no Drafts folder found", exit_code=5
        )
    except Exception as e:  # never str(e): server text can echo mail content
        _fail_audited(
            **fail,
            code="operation_error",
            message=f"IMAP operation failed ({type(e).__name__})",
            exit_code=2,
        )
    if not_found:
        _fail_audited(
            **fail, code="not_found", message=f"no message with UID {uid} in {folder}", exit_code=1
        )
    if refused is not None:
        # RecipientError's message names the rule and the way out, never an address.
        _fail_audited(**fail, code=refused.code, message=str(refused), exit_code=4)
    log_manage_action(
        subcommand="manage.draft",
        account=account.alias,
        folder=folder,
        uids=[uid],
        result="preview" if preview else "success",
        arg_keys=arg_keys,
    )
    # Recipients are read back from the built message, not from the plan, so the owner
    # sees what is (or would be) in the draft. They passed the strict address allowlist,
    # but they are still text from the mail: each one keeps its envelope, like the subject.
    out = {
        "ok": True,
        "subcommand": "manage.draft",
        "warnings": warnings,
        "subject": wrap(msg["Subject"]),
        "to": [wrap(a.addr_spec) for a in msg["To"].addresses] if msg["To"] else [],
        "cc": [wrap(a.addr_spec) for a in msg["Cc"].addresses] if msg["Cc"] else [],
    }
    if preview:
        out["preview"] = True
    else:
        out["drafts_folder"] = drafts
    _out(out)


# Ticket criterion 5: what version 1 leaves out is refused by name. Without these hidden
# options click would answer "No such option", which reads like a typo to retry in another
# spelling rather than a boundary. `--uid` is here because it is what a forwarding attempt
# passes. They take a value so that value is consumed and never echoed.
_OUT_OF_SCOPE = {
    "bcc": "--bcc",
    "attach": "--attach",
    "attachment": "--attachment",
    "forward": "--forward",
    "uid": "--uid",
}


@manage.command("compose")
@click.option("--account", "account_flag", default=None)
@click.option("--to", "to", multiple=True, help="One bare address; repeat for more.")
@click.option("--cc", "cc", multiple=True, help="One bare address; repeat for more.")
@click.option("--subject", default=None)
@click.option("--body-file", default=None, help="A text file in the outbox; see `manage outbox`.")
@click.option("--bcc", multiple=True, hidden=True)
@click.option("--attach", multiple=True, hidden=True)
@click.option("--attachment", multiple=True, hidden=True)
@click.option("--forward", multiple=True, hidden=True)
@click.option("--uid", multiple=True, hidden=True)
@click.option("--json", "json_mode", is_flag=True, help="Accepted for symmetry; output is JSON.")
def compose_cmd(
    account_flag, to, cc, subject, body_file, bcc, attach, attachment, forward, uid, json_mode
):
    """Put a NEW mail into the Drafts folder. To and Cc only. Never sends."""
    account, creds = _resolve(account_flag)
    refused = dict(bcc=bcc, attach=attach, attachment=attachment, forward=forward, uid=uid)
    arg_keys = _arg_keys(to=to, cc=cc, subject=subject, body_file=body_file, **refused)
    # No source folder exists for a new mail; the Drafts folder is recorded on success.
    fail = dict(subcommand="manage.compose", account=account, folder="", arg_keys=arg_keys)
    given = [_OUT_OF_SCOPE[k] for k, v in refused.items() if v]
    if given:
        _fail_audited(
            **fail,
            code="out_of_scope",
            message=(
                f"{', '.join(given)} is outside what `manage compose` does: it writes a new "
                "mail with To and Cc only. No Bcc, no attachment, no forwarding of an "
                "existing mail. The user adds those in their mail client"
            ),
            exit_code=4,
        )
    # Required options are checked here, not by click (`required=True`): click would print
    # usage text with exit 2, no JSON and no audit record.
    missing = [
        opt
        for opt, value in (
            ("--to", to),
            ("--subject", (subject or "").strip()),
            ("--body-file", body_file),
        )
        if not value
    ]
    if missing:
        _fail_audited(
            **fail,
            code="bad_args",
            message=f"{', '.join(missing)} is required; no draft was written",
            exit_code=4,
        )
    body = bodyfile.read_body_file(body_file)
    if body is None:
        _fail_audited(**fail, code="bad_args", message=_BODY_FILE_MESSAGE, exit_code=4)
    # The message is built before the mailbox is touched: a refused address or an
    # over-long recipient list costs no IMAP call. ComposeError names the rule only.
    try:
        msg = build_new(from_addr=account.email, to=to, cc=cc, subject=subject, body=body)
    except ComposeError as e:
        _fail_audited(**fail, code="bad_args", message=str(e), exit_code=4)
    except Exception as e:  # never str(e): it can echo an argument
        _fail_audited(
            **fail,
            code="bad_args",
            message=f"the mail could not be built ({type(e).__name__}); no draft was written",
            exit_code=4,
        )
    try:
        with imap_connect(creds, port=account.port) as mb:
            drafts = save_draft(mb, msg)
    except NoDraftsFolderError:  # never str(e) — a literal message
        _fail_audited(
            **fail, code="no_drafts_folder", message="no Drafts folder found", exit_code=5
        )
    except Exception as e:  # never str(e): server text can echo mail content
        _fail_audited(
            **fail,
            code="operation_error",
            message=f"IMAP operation failed ({type(e).__name__})",
            exit_code=2,
        )
    log_manage_action(
        subcommand="manage.compose",
        account=account.alias,
        folder=drafts,
        uids=[],
        result="success",
        arg_keys=arg_keys,
    )
    # Recipients and subject are read back from the built message, not echoed from the
    # arguments, so the skill shows the owner what is in the draft. They are this command's
    # own checked arguments, not mail content: no envelope.
    _out(
        {
            "ok": True,
            "subcommand": "manage.compose",
            "drafts_folder": drafts,
            "to": [a.addr_spec for a in msg["To"].addresses],
            "cc": [a.addr_spec for a in msg["Cc"].addresses] if msg["Cc"] else [],
            "subject": msg["Subject"],
        }
    )


def _load_playbook_result():
    """R1: a malformed sources.json is reported, never swallowed. Neither `manage
    playbooks` nor `manage playbook` touches an account or a mailbox, so a failure here
    is NOT audited (same as `_resolve` failures)."""
    try:
        return load_playbooks(public_playbooks(), load_sources())
    except SourcesConfigError as e:
        _fail("sources_config_error", str(e), 4)


@manage.command("playbooks")
@click.option("--json", "json_mode", is_flag=True, help="Accepted for symmetry; output is JSON.")
def playbooks_cmd(json_mode):
    """List every known playbook id with its recognition hints. Never touches a mailbox."""
    r = _load_playbook_result()
    _out(
        {
            "ok": True,
            "subcommand": "manage.playbooks",
            "warnings": r.warnings,
            "playbooks": [
                {
                    "id": p.id,
                    "recognition": list(p.recognition),
                    "has_overlay": p.overlay is not None,
                }
                # M1: `Path.iterdir()`-derived order is not deterministic across
                # filesystems; the listing is always sorted by id.
                for p in sorted(r.playbooks.values(), key=lambda p: p.id)
            ],
        }
    )


@manage.command("playbook")
@click.option("--id", "pid", required=True)
@click.option("--json", "json_mode", is_flag=True, help="Accepted for symmetry; output is JSON.")
def playbook_cmd(pid, json_mode):
    """Show one playbook with its overlays. Never touches a mailbox.

    R3: an unknown --id falls back to "generic", but never silently — the response
    always carries the id actually requested, plus a warning naming the fallback.
    R10: playbook body and overlay text are the owner's own data, returned un-enveloped
    (never mail content, so `wrap()` does not apply here).
    """
    r = _load_playbook_result()
    warnings = list(r.warnings)
    p = r.playbooks.get(pid)
    if p is None:
        warnings.append(f"no playbook {pid!r}; using generic")
        p = r.playbooks["generic"]
    _out(
        {
            "ok": True,
            "subcommand": "manage.playbook",
            "requested_id": pid,
            "id": p.id,
            "tone": p.tone,
            "body": p.body,
            "overlay": p.overlay.body if p.overlay else None,
            "warnings": warnings,
        }
    )
