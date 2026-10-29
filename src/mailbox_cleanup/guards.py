"""Guard library for the bot profile (#76 section 3, slice 3b).

Pure and transport-agnostic: no click, no config read, no profile read, no network.
The MCP host (3c) builds the parameters from the root-owned profile and calls these
functions before every side effect. Every refusal is a GuardRefused, which carries an
error code, an exit code and a rule name, and never a folder name, address or UID.

What holds here holds in the library. It is enforced for the bot once the host uses it.
"""

import re
from collections.abc import Iterable

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
