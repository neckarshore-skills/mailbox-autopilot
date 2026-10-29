"""Guard library for the bot profile (#76 section 3, slice 3b).

Pure and transport-agnostic: no click, no config read, no profile read, no network.
The MCP host (3c) builds the parameters from the root-owned profile and calls these
functions before every side effect. Every refusal is a GuardRefused, which carries an
error code, an exit code and a rule name, and never a folder name, address or UID.

What holds here holds in the library. It is enforced for the bot once the host uses it.
"""

import hashlib
import json
import re
import secrets
import time
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass

EXIT_CODES = {
    "preview_mismatch": 4,  # existing code: the apply does not match the preview
    "profile_refused": 6,  # the bot profile refuses the request (#89)
    "cap_reached": 7,  # a per-run or rolling 24-hour cap (#76 section 5.7)
}


class GuardRefused(Exception):
    """A guard refused the request. `rule` names the guard; the message carries no values."""

    def __init__(self, error_code: str, rule: str, message: str, *, action: str | None = None):
        if error_code not in EXIT_CODES:
            raise ValueError(f"unknown error code {error_code!r}")
        super().__init__(message)
        self.error_code = error_code
        self.rule = rule
        self.action = action

    @property
    def exit_code(self) -> int:
        return EXIT_CODES[self.error_code]

    def audit_fields(self) -> dict:
        fields = {"error_code": self.error_code, "rule": self.rule}
        if self.action is not None:
            fields["action"] = self.action
        return fields


def _refuse(rule: str, message: str, **kw) -> GuardRefused:
    return GuardRefused("profile_refused", rule, message, **kw)


def _strings(values, rule: str, what: str) -> list:
    """`values` must be a list or tuple; a str is iterable but is not a set of items."""
    if not isinstance(values, (list, tuple)):
        raise _refuse(rule, f"{what} must be a list")
    return list(values)


# --- (h) UIDs ------------------------------------------------------------------------

MAX_UID = 2**32 - 1  # RFC 3501: nz-number is a 32-bit unsigned integer
_UID_RE = re.compile(r"[0-9]+", re.ASCII)  # ASCII: str.isdigit() would accept "²"


def check_uids(values) -> tuple[str, ...]:
    """Digits only, 1..2^32-1, no ranges and no `*`. Returns canonical, unique, sorted UIDs.

    A UID set joined from raw strings would let `*` or `1:*` cover a whole folder
    (operations/batching.py); here every element is one plain number.
    """
    items = _strings(values, "uid_set", "the UID set")
    if not items:
        raise _refuse("uid_empty", "the UID set is empty")
    out: set[int] = set()
    for v in items:
        if type(v) is not str or not _UID_RE.fullmatch(v):
            raise _refuse("uid_format", "a UID must be plain ASCII digits")
        n = int(v)
        if not 1 <= n <= MAX_UID:
            raise _refuse("uid_format", "a UID must be between 1 and 2^32-1")
        out.add(n)
    return tuple(str(n) for n in sorted(out))


# --- (b) flags -----------------------------------------------------------------------

ADDABLE_SYSTEM_FLAGS = {"\\flagged": "\\Flagged", "\\seen": "\\Seen"}
FORBIDDEN_FLAGS = frozenset(
    f.casefold()
    for f in ("\\Deleted", "\\Answered", "\\Draft", "\\Recent", "$Junk", "Junk", "$NotJunk")
    + ("NonJunk", "\\Flagged", "\\Seen")
)
KEYWORD_PREFIX = "$Bot"
# a strict atom: the prefix, then 1-40 of [A-Za-z0-9_-]; nothing that can end a flag list
_KEYWORD_RE = re.compile(r"\$Bot[A-Za-z0-9_-]{1,40}", re.ASCII)


def _flag_kind(flag) -> str:
    if type(flag) is not str:
        return "invalid"
    if flag.casefold() in ADDABLE_SYSTEM_FLAGS:
        return "addable_system"
    if _KEYWORD_RE.fullmatch(flag):
        return "own_keyword"
    if flag.casefold() in FORBIDDEN_FLAGS:
        return "forbidden"
    return "invalid"


def check_flags(*, add, remove) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """The bot may add \\Flagged, \\Seen and its own `$Bot…` keywords, and remove only
    its own keywords. Everything else is refused. Returns (add, remove), canonical."""
    add_items = _strings(add, "flag_not_allowed", "flags to add")
    remove_items = _strings(remove, "flag_not_allowed", "flags to remove")
    if not add_items and not remove_items:
        raise _refuse("flag_empty", "no flag to add or remove")
    out_add: list[str] = []
    for f in add_items:
        kind = _flag_kind(f)
        if kind == "addable_system":
            canon = ADDABLE_SYSTEM_FLAGS[f.casefold()]
        elif kind == "own_keyword":
            canon = f
        elif kind == "forbidden":
            raise _refuse("flag_forbidden", "this flag may not be set by the bot", action="flag")
        else:
            raise _refuse("flag_not_allowed", "only \\Flagged, \\Seen and $Bot keywords")
        if canon not in out_add:
            out_add.append(canon)
    out_remove: list[str] = []
    for f in remove_items:
        kind = _flag_kind(f)
        if kind == "own_keyword":
            if f not in out_remove:
                out_remove.append(f)
        elif kind in ("addable_system", "forbidden"):
            raise _refuse("flag_forbidden", "the bot removes only its own keywords", action="flag")
        else:
            raise _refuse("flag_not_allowed", "the bot removes only its own keywords")
    return tuple(out_add), tuple(out_remove)


# --- (f) native MOVE -----------------------------------------------------------------


def require_native_move(capabilities: Iterable[str]) -> None:
    """The bot path needs native MOVE. UIDPLUS alone is not enough: the COPY, STORE and
    EXPUNGE fallback in operations/batching.py is not available to the bot."""
    if "MOVE" not in {str(c).upper() for c in capabilities}:
        raise _refuse("move_not_native", "the server has no native MOVE", action="move")


# --- (a) (a2) (g) folders ------------------------------------------------------------

MAX_FOLDER_NAME = 200
# RFC 6154 SPECIAL-USE attributes, compared case-folded
SPECIAL_USE = frozenset(
    f.casefold()
    for f in ("\\All", "\\Archive", "\\Drafts", "\\Flagged", "\\Junk", "\\Sent", "\\Trash")
)
# Names that clients and server retention rules treat as trash or junk without any
# SPECIAL-USE flag (#76 section 3a). Compared per path segment, NFC and case-folded.
TRASH_OR_JUNK_NAMES = frozenset(
    unicodedata.normalize("NFC", n).casefold()
    for n in (
        "Bin",
        "Bulk Mail",
        "Deleted",
        "Deleted Items",
        "Deleted Messages",
        "Gelöschte Elemente",
        "Gelöschte Objekte",
        "Junk",
        "Junk E-Mail",
        "Junk-E-Mail",
        "Junk Mail",
        "Papierkorb",
        "Spam",
        "Trash",
        "Unerwünscht",
        "Unerwuenscht",
    )
)


def _fold(s: str) -> str:
    return unicodedata.normalize("NFC", s).casefold()


def check_folder_name(name) -> str:
    """No control characters, no IMAP wildcards, bounded length (#76 section 2.6)."""
    if (
        type(name) is not str
        or not 1 <= len(name) <= MAX_FOLDER_NAME
        or not name.isprintable()
        or "*" in name
        or "%" in name
    ):
        raise _refuse("folder_name", "the folder name is not allowed")
    return name


@dataclass(frozen=True)
class Folder:
    name: str
    delim: str | None
    flags: frozenset[str]


class FolderView:
    """The server's LIST result, seen through the bot profile.

    Targets: inside the namespace, or owner-listed. Sources: INBOX or the namespace.
    Neither may carry a SPECIAL-USE flag or a trash or junk name in any segment.
    New folders: inside the namespace only. The LIST result stays inside: `visible()`
    is the only list of names the host may return.
    """

    def __init__(self, entries: Iterable, *, namespace, owner_targets: Iterable[str] = ()):
        self._folders: dict[str, Folder] = {}
        for e in entries:
            flags = frozenset(str(f).casefold() for f in (e.flags or ()))
            self._folders[e.name] = Folder(e.name, e.delim, flags)
        inbox = self._get("INBOX")
        delims = [f.delim for f in self._folders.values() if f.delim]
        self.delim = (inbox.delim if inbox and inbox.delim else None) or (
            delims[0] if delims else None
        )
        try:
            check_folder_name(namespace)
        except GuardRefused:
            raise _refuse("namespace_invalid", "the bot namespace is not a valid name") from None
        if _fold(namespace) == "inbox" or self._trash_like(namespace):
            raise _refuse("namespace_invalid", "the bot namespace may not be INBOX or a trash")
        ns = self._folders.get(namespace)
        if ns is not None and ns.flags & SPECIAL_USE:
            raise _refuse("namespace_invalid", "the bot namespace carries a SPECIAL-USE flag")
        self.namespace = namespace
        self.owner_targets = tuple(check_folder_name(t) for t in owner_targets)

    # helpers

    def _get(self, name: str) -> Folder | None:
        if _fold(name) == "inbox":  # RFC 3501: INBOX is case-insensitive
            return next((f for f in self._folders.values() if _fold(f.name) == "inbox"), None)
        return self._folders.get(name)

    def _segments(self, name: str) -> list[str]:
        return name.split(self.delim) if self.delim else [name]

    def _trash_like(self, name: str) -> bool:
        return any(_fold(s) in TRASH_OR_JUNK_NAMES for s in self._segments(name))

    def in_namespace(self, name: str) -> bool:
        if name == self.namespace:
            return True
        return bool(self.delim) and name.startswith(self.namespace + self.delim)

    def _existing(self, name) -> Folder:
        f = self._get(check_folder_name(name))
        if f is None:
            raise _refuse("folder_unknown", "the folder is not in the server's LIST")
        return f

    # the checks

    def check_target(self, name) -> str:
        f = self._existing(name)
        if not (self.in_namespace(f.name) or f.name in self.owner_targets):
            raise _refuse("target_outside_namespace", "targets are the namespace or owner-listed")
        if f.flags & SPECIAL_USE:
            raise _refuse("target_special_use", "the target carries a SPECIAL-USE flag")
        if self._trash_like(f.name):
            raise _refuse("target_trash_like", "the target has a trash or junk name")
        return f.name

    def check_source(self, name) -> str:
        f = self._existing(name)
        inbox = _fold(f.name) == "inbox"
        if not (inbox or self.in_namespace(f.name)):
            raise _refuse("source_not_allowed", "sources are INBOX and the namespace")
        if not inbox and (f.flags & SPECIAL_USE or self._trash_like(f.name)):
            raise _refuse("source_forbidden", "no move out of trash, junk, sent, drafts or all")
        return f.name

    def check_new_folder(self, name) -> str:
        check_folder_name(name)
        if not self.in_namespace(name):
            raise _refuse("folder_outside_namespace", "new folders go inside the namespace only")
        if any(s == "" for s in self._segments(name)):
            raise _refuse("folder_name", "the folder name has an empty segment")
        if self._get(name) is not None:
            raise _refuse("folder_exists", "the folder exists already")
        if self._trash_like(name):
            raise _refuse("folder_trash_like", "a new folder may not have a trash or junk name")
        if name != self.namespace:
            parent = name.rsplit(self.delim, 1)[0]
            if self._get(parent) is None:
                raise _refuse("folder_parent_missing", "the parent folder does not exist")
        return name

    def visible(self) -> tuple[str, ...]:
        """The only folder names the host may return (#76 section 2.7)."""
        return tuple(
            sorted(
                f.name
                for f in self._folders.values()
                if _fold(f.name) == "inbox"
                or self.in_namespace(f.name)
                or f.name in self.owner_targets
            )
        )


# --- (d) preview token ---------------------------------------------------------------

TOKEN_LIFETIME_SECONDS = 300  # preview and apply happen in one run of the bot
_TOKEN_RE = re.compile(r"[0-9a-f]{64}", re.ASCII)
PLAN_ACTIONS = ("flag", "move")


def _mismatch(rule: str, message: str) -> GuardRefused:
    return GuardRefused("preview_mismatch", rule, message)


@dataclass(frozen=True)
class Plan:
    """What a preview showed. The apply gets everything from here, nothing from the caller."""

    action: str
    account: str
    source: str
    uidvalidity: int
    uids: tuple[str, ...]
    target: str | None = None
    flags_add: tuple[str, ...] = ()
    flags_remove: tuple[str, ...] = ()


def _checked_plan(plan: Plan) -> Plan:
    uids = check_uids(list(plan.uids))
    bad = (
        plan.action not in PLAN_ACTIONS
        or type(plan.uidvalidity) is not int
        or plan.uidvalidity < 1
        or type(plan.account) is not str
        or type(plan.source) is not str
        or (plan.action == "move" and (type(plan.target) is not str or plan.flags_add))
        or (plan.action == "move" and plan.flags_remove)
        or (plan.action == "flag" and (plan.target is not None))
        or (plan.action == "flag" and not (plan.flags_add or plan.flags_remove))
    )
    if bad:
        raise _refuse("plan_invalid", "the preview does not describe one move or one flag change")
    flags_add, flags_remove = plan.flags_add, plan.flags_remove
    if plan.action == "flag":
        flags_add, flags_remove = check_flags(add=list(flags_add), remove=list(flags_remove))
    return Plan(
        **{**plan.__dict__, "uids": uids, "flags_add": flags_add, "flags_remove": flags_remove}
    )


class PreviewLedger:
    """Preview tokens for one host process. A token is a hash over a random nonce and the
    plan; the ledger keeps the plan, so the apply needs nothing but the token. Single use:
    a token is spent by its first redeem, whether that succeeds or not."""

    def __init__(self, *, lifetime: float = TOKEN_LIFETIME_SECONDS, clock=time.monotonic):
        self._lifetime = lifetime
        self._clock = clock
        self._plans: dict[str, tuple[Plan, float]] = {}

    def issue(self, plan: Plan) -> str:
        plan = _checked_plan(plan)
        now = self._clock()
        self._plans = {t: e for t, e in self._plans.items() if e[1] >= now}
        canonical = json.dumps(plan.__dict__, sort_keys=True, separators=(",", ":"))
        token = hashlib.sha256(secrets.token_bytes(16) + canonical.encode()).hexdigest()
        self._plans[token] = (plan, now + self._lifetime)
        return token

    def redeem(self, token, *, action: str, account: str) -> Plan:
        if type(token) is not str or not _TOKEN_RE.fullmatch(token):
            raise _mismatch("token_invalid", "the preview token is malformed")
        entry = self._plans.pop(token, None)
        if entry is None:
            raise _mismatch("token_unknown", "no preview issued this token, or it was used")
        plan, expires = entry
        if self._clock() > expires:
            raise _mismatch("token_expired", "the preview token has expired")
        if plan.action != action:
            raise _mismatch("token_action", "the preview was for another action")
        if plan.account != account:
            raise _mismatch("token_account", "the preview was for another account")
        return plan


def check_uidvalidity(plan: Plan, current: int) -> None:
    """A new UIDVALIDITY means the same numbers name different messages."""
    if current != plan.uidvalidity:
        raise _mismatch("uidvalidity_changed", "the source folder's UIDVALIDITY changed")


def check_uids_present(plan: Plan, present: Iterable[str]) -> None:
    """Every previewed UID must still be in the source; otherwise nothing is applied."""
    if set(plan.uids) - {str(u) for u in present}:
        raise _mismatch("uids_gone", "a previewed message is no longer in the source")
