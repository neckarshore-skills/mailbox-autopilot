"""The bot's tools (#76 sections 2 and 4, slice 3c-2): lean `search`, `read` and `thread`.

Release 1 is a read-only bot (owner decision 2026-11-02, #76). These three are all the
host offers, each takes this path, and it writes nothing:

1. Every argument is checked against its own shape before anything else happens. An
   unknown key is refused, so there is no parameter that asks for a fuller read: lean is
   what the code does, not what a call asks for. Rules and messages are program
   constants and carry no value.
2. The password comes from the macOS Keychain, and the address and the server from the
   root-owned profile. Never through `auth.get_credentials`, which reads the server from
   the keychain and falls back to a fixed provider host, and never through
   `keyring.get_password`, which loads whatever backend the environment names into this
   process.
3. TLS is requested explicitly, so `MAILBOX_CLEANUP_SSL` has no say. Login selects no
   folder, and the folder is opened with EXAMINE, so the server itself refuses a change
   and the mail stays unread.
4. The folder is checked against a fresh LIST (`FolderView.check_readable`).
"""

from __future__ import annotations

import datetime
import re
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass

from keyring.backends.macOS import Keyring

from .auth import SERVICE_NAME, Credentials
from .guards import FolderView, GuardRefused, check_uids
from .imap_client import imap_connect
from .lean import SIZE_BUCKETS, Lean, search_criteria, thread_uids

MAX_LIMIT = 100
DEFAULT_LIMIT = 20
DEFAULT_FOLDER = "INBOX"
_DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", re.ASCII)
# Refused under their own rule, because a burst of them reads as an attempt: each would
# be an oracle for a field that is never returned (section 4.4, and the contract of #94)
_REFUSED_CRITERIA = frozenset(
    {
        "bcc",
        "body",
        "cc",
        "from",
        "header",
        "larger",
        "sender",
        "smaller",
        "subject",
        "text",
        "to",
    }
)
_SEARCH_ARGS = frozenset(
    {"answered", "before", "flagged", "folder", "limit", "seen", "since", "size"}
)
_UID_ARGS = frozenset({"folder", "uid"})
_FOLDER_SCHEMA = {
    "type": "string",
    "description": "INBOX, or a folder in the bot's namespace. Default INBOX.",
}
_UID_SCHEMA = {"type": "string", "pattern": "^[0-9]+$", "description": "One message UID."}
_DATE_SCHEMA = {"type": "string", "pattern": "^[0-9]{4}-[0-9]{2}-[0-9]{2}$"}


@dataclass(frozen=True)
class Tool:
    spec: dict
    run: Callable[[dict], dict]


def _refuse(rule: str, message: str) -> GuardRefused:
    return GuardRefused("profile_refused", rule, message)


# --- arguments -------------------------------------------------------------------------


def _only(args: dict, allowed: frozenset[str], refused: frozenset[str] = frozenset()) -> None:
    if set(args) & refused:
        raise _refuse("search_criterion_refused", "search takes date, flag and size criteria only")
    if set(args) - allowed:
        raise _refuse("argument_unknown", "this tool does not take that argument")


def _invalid(what: str) -> GuardRefused:
    return _refuse("argument_invalid", f"{what} is not valid")


def _date(args: dict, key: str) -> datetime.date | None:
    if key not in args:
        return None
    value = args[key]
    if type(value) is not str or not _DATE_RE.fullmatch(value):
        raise _invalid("the date")
    try:
        return datetime.date.fromisoformat(value)
    except ValueError:
        raise _invalid("the date") from None


def _flag(args: dict, key: str) -> bool | None:
    if key not in args:
        return None
    if type(args[key]) is not bool:
        raise _invalid("the flag criterion")
    return args[key]


def _limit(args: dict) -> int:
    value = args.get("limit", DEFAULT_LIMIT)
    if type(value) is not int or not 1 <= value <= MAX_LIMIT:  # no bool, no float
        raise _invalid("the limit")
    return value


def _bucket(args: dict) -> str | None:
    if "size" not in args:
        return None
    value = args["size"]
    if type(value) is not str or value not in SIZE_BUCKETS:
        raise _invalid("the size")
    return value


def _uid(args: dict) -> str:
    return check_uids([args.get("uid")])[0]


# --- the mailbox -----------------------------------------------------------------------


def _keychain() -> Keyring:
    """The macOS Keychain, by class. `keyring.get_keyring()` would read
    PYTHON_KEYRING_BACKEND and keyringrc.cfg and import what they name. The backend's own
    constructor sets attributes from KEYRING_PROPERTY_*, and its class reads KEYCHAIN_PATH
    at import: both are cleared here, because env may only tighten (section 1.4)."""
    backend = Keyring()
    backend.__dict__.clear()
    backend.keychain = None
    return backend


def _password(email: str) -> str:
    try:
        password = _keychain().get_password(SERVICE_NAME, email)
    except Exception:
        raise _refuse("credential_unavailable", "the keychain cannot answer") from None
    if not password:
        raise _refuse("credential_missing", "the bot's keychain holds no password")
    return password


@contextmanager
def _opened(p, folder):
    """One connection for one call, with `folder` opened read-only, or a refusal.

    `max_retries=0`: the next run is the retry. There is no sleep inside a tool call, and
    a revoked password cannot lock the account three logins at a time."""
    creds = Credentials(email=p.email, password=_password(p.email), server=p.server)
    with imap_connect(creds, ssl=True, max_retries=0, select_inbox=False) as mb:
        view = FolderView(mb.folder.list(), namespace=p.namespace, owner_targets=p.owner_targets)
        mb.folder.set(view.check_readable(folder), readonly=True)
        yield mb


def _answer(rows: list[dict], truncated: bool) -> dict:
    return {"count": len(rows), "messages": rows, "truncated": truncated}


# --- the tools -------------------------------------------------------------------------


def build(p) -> dict[str, Tool]:
    """The tools for this profile: the three names of release 1, nothing else."""
    lean = Lean(keep=p.keep, authserv_id=p.authserv_id, suppress=p.suppress)

    def search(args: dict) -> dict:
        _only(args, _SEARCH_ARGS, _REFUSED_CRITERIA)
        criteria = search_criteria(
            since=_date(args, "since"),
            before=_date(args, "before"),
            seen=_flag(args, "seen"),
            flagged=_flag(args, "flagged"),
            answered=_flag(args, "answered"),
            size=_bucket(args),
        )
        limit = _limit(args)
        with _opened(p, args.get("folder", DEFAULT_FOLDER)) as mb:
            return _answer(*lean.search(mb, criteria, limit))

    def read(args: dict) -> dict:
        _only(args, _UID_ARGS)
        uid = _uid(args)
        with _opened(p, args.get("folder", DEFAULT_FOLDER)) as mb:
            return _answer(lean.rows(mb, [uid]), False)

    def thread(args: dict) -> dict:
        _only(args, _UID_ARGS)
        uid = _uid(args)
        with _opened(p, args.get("folder", DEFAULT_FOLDER)) as mb:
            if not lean.rows(mb, [uid]):  # no mail, or a suppressed one: the same answer
                return _answer([], False)
            uids, truncated = thread_uids(mb, uid)
            return _answer(lean.rows(mb, uids), truncated)

    by_uid = {"uid": _UID_SCHEMA, "folder": _FOLDER_SCHEMA}
    return {
        "read": Tool(
            spec={
                "name": "read",
                "description": "The metadata record of one message. Never its text.",
                "inputSchema": _schema(by_uid, required=["uid"]),
            },
            run=read,
        ),
        "search": Tool(
            spec={
                "name": "search",
                "description": (
                    "Metadata records of the newest matching messages. Filters on arrival "
                    "date, flags and size bucket only."
                ),
                "inputSchema": _schema(
                    {
                        "answered": {"type": "boolean"},
                        "before": _DATE_SCHEMA,
                        "flagged": {"type": "boolean"},
                        "folder": _FOLDER_SCHEMA,
                        "limit": {"type": "integer", "minimum": 1, "maximum": MAX_LIMIT},
                        "seen": {"type": "boolean"},
                        "since": _DATE_SCHEMA,
                        "size": {"type": "string", "enum": sorted(SIZE_BUCKETS)},
                    }
                ),
            },
            run=search,
        ),
        "thread": Tool(
            spec={
                "name": "thread",
                "description": "Metadata records of the messages in one message's thread.",
                "inputSchema": _schema(by_uid, required=["uid"]),
            },
            run=thread,
        ),
    }


def _schema(properties: dict, required: list[str] | None = None) -> dict:
    schema = {"type": "object", "properties": properties, "additionalProperties": False}
    return {**schema, "required": required} if required else schema
