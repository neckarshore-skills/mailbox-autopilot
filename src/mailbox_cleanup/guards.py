"""Guard library for the bot profile (#76 section 3, slice 3b).

Pure and transport-agnostic: no click, no config read, no profile read, no network.
The MCP host (3c) builds the parameters from the root-owned profile and calls these
functions before every side effect. Every refusal is a GuardRefused, which carries an
error code, an exit code and a rule name, and never a folder name, address or UID.

What holds here holds in the library. It is enforced for the bot once the host uses it.
"""

import errno
import fcntl
import hashlib
import json
import os
import re
import secrets
import stat
import threading
import time
import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

from .operations.selection import _is_kept, parse_keep

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


def _fold(s: str) -> str:
    """NFKC, case-folded, whitespace runs collapsed: fullwidth or doubled-space forms of
    a trash name compare equal to it (security review of 3b)."""
    return " ".join(unicodedata.normalize("NFKC", s).casefold().split())


# Names that clients and server retention rules treat as trash or junk without any
# SPECIAL-USE flag (#76 section 3a). Compared per path segment, folded by _fold.
TRASH_OR_JUNK_NAMES = frozenset(
    _fold(n)
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


def _is_inbox(name: str) -> bool:
    """RFC 3501: INBOX is case-insensitive, ASCII only; no compatibility forms."""
    return name.isascii() and name.upper() == "INBOX"


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
        try:
            self._check_segments(namespace)
        except GuardRefused:
            raise _refuse("namespace_invalid", "the bot namespace is not a valid name") from None
        if _is_inbox(namespace) or self._trash_like(namespace):
            raise _refuse("namespace_invalid", "the bot namespace may not be INBOX or a trash")
        ns = self._folders.get(namespace)
        if ns is not None and ns.flags & SPECIAL_USE:
            raise _refuse("namespace_invalid", "the bot namespace carries a SPECIAL-USE flag")
        self.namespace = namespace
        self.owner_targets = tuple(check_folder_name(t) for t in owner_targets)

    # helpers

    def _get(self, name: str) -> Folder | None:
        if _is_inbox(name):  # RFC 3501: INBOX is case-insensitive
            return next((f for f in self._folders.values() if _is_inbox(f.name)), None)
        return self._folders.get(name)

    def _segments(self, name: str) -> list[str]:
        return name.split(self.delim) if self.delim else [name]

    def _check_segments(self, name: str) -> None:
        """No empty segment and no segment with an edge space, which would hide a name."""
        if any(s == "" or s != s.strip() for s in self._segments(name)):
            raise _refuse("folder_name", "a folder name segment is empty or has an edge space")

    def _trash_like(self, name: str) -> bool:
        return any(_fold(s) in TRASH_OR_JUNK_NAMES for s in self._segments(name))

    def in_namespace(self, name: str) -> bool:
        if name == self.namespace:
            return True
        return bool(self.delim) and name.startswith(self.namespace + self.delim)

    def _existing(self, name) -> Folder:
        self._check_segments(check_folder_name(name))
        f = self._get(name)
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
        inbox = _is_inbox(f.name)
        if not (inbox or self.in_namespace(f.name)):
            raise _refuse("source_not_allowed", "sources are INBOX and the namespace")
        if not inbox and (f.flags & SPECIAL_USE or self._trash_like(f.name)):
            raise _refuse("source_forbidden", "no move out of trash, junk, sent, drafts or all")
        return f.name

    def check_new_folder(self, name) -> str:
        check_folder_name(name)
        if not self.in_namespace(name):
            raise _refuse("folder_outside_namespace", "new folders go inside the namespace only")
        self._check_segments(name)
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
                if _is_inbox(f.name) or self.in_namespace(f.name) or f.name in self.owner_targets
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
    uids = check_uids(plan.uids)  # list() would turn the str "12" into UIDs 1 and 2
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
        self._lock = threading.Lock()

    def issue(self, plan: Plan) -> str:
        plan = _checked_plan(plan)
        canonical = json.dumps(plan.__dict__, sort_keys=True, separators=(",", ":"))
        token = hashlib.sha256(secrets.token_bytes(16) + canonical.encode()).hexdigest()
        with self._lock:
            now = self._clock()
            for expired in [t for t, e in self._plans.items() if e[1] < now]:
                del self._plans[expired]  # in place, so a concurrent pop cannot be undone
            self._plans[token] = (plan, now + self._lifetime)
        return token

    def redeem(self, token, *, action: str, account: str) -> Plan:
        if type(token) is not str or not _TOKEN_RE.fullmatch(token):
            raise _mismatch("token_invalid", "the preview token is malformed")
        with self._lock:
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


# --- (d) caps ------------------------------------------------------------------------

ACTION_CLASSES = ("create_folder", "draft", "flag", "move")
# per run: move 50 and flag 10 from the earlier design; folder creation and drafts are
# the builder's choice (#76 section 3d). Per rolling 24 hours: 0 for every write (D7).
FIRST_ROLLOUT_PER_RUN = {"create_folder": 5, "draft": 5, "flag": 10, "move": 50}
FIRST_ROLLOUT_PER_DAY = {c: 0 for c in ACTION_CLASSES}
DAY_SECONDS = 24 * 60 * 60


def _cap_hit(action: str, rule: str, message: str) -> GuardRefused:
    return GuardRefused("cap_reached", rule, message, action=action)


def _checked_caps(caps) -> dict[str, int]:
    if (
        not isinstance(caps, Mapping)
        or set(caps) != set(ACTION_CLASSES)
        or any(type(v) is not int or v < 0 for v in caps.values())
    ):
        raise _refuse("caps_invalid", "caps must name every action class with a count >= 0")
    return dict(caps)


def _trusted_dir(resolved: Path) -> bool:
    """Every directory from `resolved` up to `/` is owned by root or the caller, and group
    and others either cannot write it or it is sticky, so nobody else can swap the path
    below it. In a sticky directory (as /tmp, 1777) only an entry's owner, the
    directory's owner and root can rename or delete the entry; the entry is then checked
    like every other directory on the way."""
    me = os.getuid()
    for d in (resolved, *resolved.parents):
        st = os.lstat(d)
        shared = st.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        if (
            not stat.S_ISDIR(st.st_mode)
            or st.st_uid not in (0, me)
            or (shared and not st.st_mode & stat.S_ISVTX)
        ):
            return False
    return True


def _is_append_only(st) -> bool:
    """The system append-only flag (`chflags sappend`), which only root can set or clear.
    The user flag does not count, because the owner can clear it. Without st_flags
    (Linux) there is no such flag, so the store refuses."""
    return bool(getattr(st, "st_flags", 0) & stat.SF_APPEND)


class CapLedger:
    """Per-run and rolling 24-hour caps per action class (#76 section 3d).

    `reserve` is called before the side effect and records it; a failed side effect
    stays counted, which errs on the safe side. A request above what remains is refused
    whole, never cut down. A daily cap of 0 refuses before the store is touched.

    With a cap above 0 the store must be the caller's own regular file with one link,
    that group and others cannot write, and that carries the system append-only flag, so
    the bot can add counts but cannot truncate or delete the store (#76 D15). Every
    directory above it must be root's or the caller's, and group and others cannot write
    it unless it is sticky. Anything else refuses, each with its own rule. `reserve`
    never creates the store, so deleting it does not start a new day: the launcher
    provisions it once (`provision`) and root sets `chflags sappend`. Where the store
    lives is the host's and launcher's decision (3c); section 5.5 accepts a same-uid
    store only once 5b is proven.
    """

    @staticmethod
    def provision(store: Path) -> None:
        """Create an empty owner-only store. Refuses (FileExistsError) if anything is there.

        The store belongs to whoever runs this, and `reserve` accepts only a store the
        bot owns. So it runs as the bot uid, or the launcher, which runs as root (L3),
        chowns the file to the bot before root sets `chflags sappend`.
        """
        fd = os.open(store, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            os.fchmod(fd, 0o600)
        finally:
            os.close(fd)

    def __init__(self, *, per_run, per_day, store: Path, run_id: str, clock=time.time):
        self._per_run = _checked_caps(per_run)
        self._per_day = _checked_caps(per_day)
        self._store = Path(store)
        self._run_id = str(run_id)
        self._clock = clock
        self._used = {c: 0 for c in ACTION_CLASSES}

    def reserve(self, action, count) -> None:
        if action not in ACTION_CLASSES:
            raise _refuse("action_unknown", "no such action class")
        if type(count) is not int or count < 1:
            raise _refuse("cap_count", "the count must be a positive integer", action=action)
        if self._used[action] + count > self._per_run[action]:
            raise _cap_hit(action, f"{action}_per_run", "the per-run cap is reached")
        day_cap = self._per_day[action]
        if count > day_cap:  # covers a cap of 0 without opening the store
            raise _cap_hit(action, f"{action}_per_day", "the 24-hour cap is reached")
        fd = self._open_store()
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            now = self._clock()
            if self._used_today(fd, action, now) + count > day_cap:
                raise _cap_hit(action, f"{action}_per_day", "the 24-hour cap is reached")
            record = {"ts": now, "action": action, "count": count, "run": self._run_id}
            os.write(fd, (json.dumps(record, sort_keys=True) + "\n").encode())
        finally:
            os.close(fd)
        self._used[action] += count

    def _open_store(self) -> int:
        flags = os.O_RDWR | os.O_APPEND | os.O_NOFOLLOW  # no O_CREAT: a missing store refuses
        try:
            parent = self._store.parent.resolve(strict=True)
        except OSError:
            raise _refuse("cap_store_dir_missing", "the cap store's directory is missing") from None
        if not _trusted_dir(parent):
            raise _refuse(
                "cap_store_dir_untrusted", "a directory above the cap store is not trusted"
            )
        try:
            fd = os.open(parent / self._store.name, flags)
        except OSError as e:
            if e.errno == errno.ELOOP:
                raise _refuse("cap_store_symlink", "the cap store is a symlink") from None
            if e.errno == errno.ENOENT:
                raise _refuse("cap_store_missing", "the cap store does not exist") from None
            raise _refuse("cap_store_unopenable", "the cap store cannot be opened") from None
        st = os.fstat(fd)
        if (
            not stat.S_ISREG(st.st_mode)
            or st.st_uid != os.getuid()
            or st.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
            or st.st_nlink != 1
        ):
            os.close(fd)
            raise _refuse("cap_store_file_untrusted", "the cap store is not an owner-only file")
        if not _is_append_only(st):  # checked on the opened descriptor, after the rest
            os.close(fd)
            raise _refuse(
                "cap_store_not_append_only", "the cap store lacks the system append-only flag"
            )
        return fd

    @staticmethod
    def _used_today(fd: int, action: str, now: float) -> int:
        os.lseek(fd, 0, os.SEEK_SET)
        chunks = []
        while chunk := os.read(fd, 65536):
            chunks.append(chunk)
        total = 0
        try:
            for line in b"".join(chunks).decode("utf-8").splitlines():
                r = json.loads(line)
                ts, cls, n = r["ts"], r["action"], r["count"]
                if type(ts) not in (int, float) or type(n) is not int or cls not in ACTION_CLASSES:
                    raise ValueError("bad record")
                if cls == action and ts > now - DAY_SECONDS:  # a future entry counts too
                    total += n
        except (ValueError, KeyError, TypeError, UnicodeDecodeError):
            raise _refuse("cap_store_corrupt", "the cap store is corrupt") from None
        return total


# --- (c) keep list at the last step --------------------------------------------------


# one bare addr-spec: one @, a non-empty local part and domain, no space, no <>,;"()[]\
_BARE_ADDRESS_RE = re.compile(r'[^@\s<>,;"()\[\]\\]+@[^@\s<>,;"()\[\]\\]+')


def ensure_none_kept(uids: Iterable[str], senders: Mapping, keep: Iterable[str]) -> None:
    """Refuse if any UID's sender is on the keep list, or cannot be read.

    Called with the From addresses fetched for exactly these UIDs immediately before
    MOVE or STORE, so a path that skips the selection layer cannot get around the keep
    list. A keep-list hit is not authentication: From can be spoofed. The refusal names
    no address.

    Contract: `senders` maps each UID to one parsed, bare address (as imap_tools'
    `from_`, which the selection layer passes). A display name, several mailboxes or
    anything else that is not one bare address refuses, so the match cannot fail open.
    """
    try:
        addrs, domains = parse_keep(list(keep))
    except ValueError:
        raise _refuse("keep_invalid", "the keep list has an invalid entry") from None
    for uid in uids:
        sender = senders.get(uid)
        if type(sender) is not str or not _BARE_ADDRESS_RE.fullmatch(sender):
            raise _refuse("keep_unverifiable", "a message's sender cannot be read")
        if _is_kept(sender, addrs, domains):
            raise _refuse("keep_list", "a message is from a sender on the keep list")
