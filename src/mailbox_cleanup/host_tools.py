"""The bot's tools (#76 sections 2 and 4, slice 3c-2): lean `search` and `read`.

Release 1 is a read-only bot that reads the INBOX's metadata (owner decisions 2026-11-02,
#76). These two are all the host offers, and each takes this path:

1. The password is read once, when the tools are built, so a host that cannot get it
   does not serve. It comes from the macOS Keychain by class, and the address and the
   server come from the root-owned profile. Never through `auth.get_credentials`, which
   reads the server from the keychain and falls back to a fixed provider host, and never
   through `keyring.get_password`, whose `get_keyring` imports what `keyringrc.cfg` or
   `PYTHON_KEYRING_BACKEND` names into the process that holds the credential.
2. Every argument is checked against its own shape before the server is touched. An
   unknown key is refused, so no parameter asks for another folder or a fuller read:
   lean is what the code does, not what a call asks for. Rules and messages are program
   constants and carry no value.
3. TLS is requested explicitly, so `MAILBOX_CLEANUP_SSL` has no say. Login selects no
   folder and the INBOX is opened with EXAMINE, so the server itself refuses a change and
   the mail stays unread. No LIST is sent: the bot learns no folder name.
"""

from __future__ import annotations

import datetime
import re
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass

from keyring.backends.macOS import Keyring

from .auth import SERVICE_NAME, Credentials
from .guards import GuardRefused, check_uids
from .imap_client import imap_connect
from .lean import SIZE_BUCKETS, Lean, search_criteria

MAX_LIMIT = 100
DEFAULT_LIMIT = 20
INBOX = "INBOX"
_DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", re.ASCII)
_SEARCH_ARGS = frozenset({"flagged", "limit", "since", "size", "unseen"})
_READ_ARGS = frozenset({"uid"})
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


# --- the mailbox -----------------------------------------------------------------------


def _keychain() -> Keyring:
    """The macOS Keychain, by class. The backend's constructor sets attributes from
    KEYRING_PROPERTY_*, and its class reads KEYCHAIN_PATH at import: both are cleared,
    because env may only tighten (section 1.4)."""
    backend = Keyring()
    backend.__dict__.clear()
    backend.keychain = None
    return backend


def read_password(email: str) -> str:
    """The bot's password, or a refusal. No retry and no fallback: a run without its
    password does not start (James's read of the cut, contract 9)."""
    try:
        password = _keychain().get_password(SERVICE_NAME, email)
    except Exception:
        password = None
    if not password:
        raise _refuse("credential_unavailable", "the bot's keychain gives no password")
    return password


@contextmanager
def _inbox(creds: Credentials):
    """One connection for one call, with the INBOX open read-only.

    `max_retries=0`: the next run is the retry. There is no sleep inside a tool call, and
    a revoked password cannot lock the account three logins at a time."""
    with imap_connect(creds, ssl=True, max_retries=0, select_inbox=False) as mb:
        mb.folder.set(INBOX, readonly=True)
        yield mb


def _answer(rows: list[dict], truncated: bool) -> dict:
    return {"count": len(rows), "messages": rows, "truncated": truncated}


# --- the tools -------------------------------------------------------------------------


def _schema(properties: dict, required: list[str] | None = None) -> dict:
    schema = {"type": "object", "properties": properties, "additionalProperties": False}
    return {**schema, "required": required} if required else schema


def build(p) -> dict[str, Tool]:
    """The tools for this profile: the two names of release 1. Raises GuardRefused when
    the password cannot be read, so the host refuses to start."""
    creds = Credentials(email=p.email, password=read_password(p.email), server=p.server)
    lean = Lean(keep=p.keep, authserv_id=p.authserv_id, suppress=p.suppress)

    def search(args: dict) -> dict:
        _only(args, _SEARCH_ARGS, _REFUSED_CRITERIA)
        criteria = search_criteria(
            since=_date(args, "since"),
            unseen=_flag(args, "unseen"),
            flagged=_flag(args, "flagged"),
            size=_bucket(args),
        )
        limit = _limit(args)
        with _inbox(creds) as mb:
            return _answer(*lean.search(mb, criteria, limit))

    def read(args: dict) -> dict:
        _only(args, _READ_ARGS)
        uid = check_uids([args.get("uid")])[0]
        with _inbox(creds) as mb:
            return _answer(lean.rows(mb, [uid]), False)

    return {
        "read": Tool(
            spec={
                "name": "read",
                "description": "The metadata record of one INBOX message. Never its text.",
                "inputSchema": _schema(
                    {"uid": {"type": "string", "pattern": "^[0-9]+$"}}, required=["uid"]
                ),
            },
            run=read,
        ),
        "search": Tool(
            spec={
                "name": "search",
                "description": (
                    "Metadata records of the newest matching INBOX messages. Filters on "
                    "arrival date, flags and size bucket only."
                ),
                "inputSchema": _schema(
                    {
                        "flagged": {"type": "boolean"},
                        "limit": {"type": "integer", "minimum": 1, "maximum": MAX_LIMIT},
                        "since": {"type": "string", "pattern": "^[0-9]{4}-[0-9]{2}-[0-9]{2}$"},
                        "size": {"type": "string", "enum": sorted(SIZE_BUCKETS)},
                        "unseen": {"type": "boolean"},
                    }
                ),
            },
            run=search,
        ),
    }
