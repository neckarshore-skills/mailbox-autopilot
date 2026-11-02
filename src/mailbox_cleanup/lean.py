"""The bot's lean view of a mailbox (#76 section 4, slice 3c-2).

Everything here serves one promise: what a mail wrote reaches the model only as a word
from a closed list or a value of a strict shape, so no header can carry an instruction.
Three things hold it:

- **Only the headers this module needs are fetched.** Subject, To, Received and the rest
  never enter the process, so no later bug can return them. Nothing here fetches a body.
- **Every value is reduced, never echoed.** A header either becomes a boolean, a word
  from a closed list, one normalised domain, or nothing.
- **No server search takes a value from the model or from a mail.** The only criteria
  are dates, flags and size buckets, built here from typed values. `FROM` would be an
  oracle for a field that is never returned, because it matches the display name, and
  byte-level `LARGER` would be one for the exact size. `thread` follows the same rule: it
  compares Message-IDs here, not on the server.

Nothing here knows the profile or the host. The caller passes the keep list and the
authserv-id, and validates its arguments before it gets here.
"""

from __future__ import annotations

import datetime
import re
from collections.abc import Iterable, Sequence
from email.message import Message
from email.parser import HeaderParser
from email.policy import compat32
from email.utils import getaddresses

from .classify import classify
from .guards import _BARE_ADDRESS_RE
from .manage.read import parse_message_ids
from .manage.search import _fetch, _Row, _uid_cmd, _uid_list
from .operations.selection import _is_kept, parse_keep

LEAN_HEADERS = (
    "AUTHENTICATION-RESULTS",
    "AUTO-SUBMITTED",
    "CONTENT-TYPE",
    "FROM",
    "LIST-ID",
    "LIST-UNSUBSCRIBE",
    "PRECEDENCE",
)
_LEAN_PART = f"RFC822.SIZE BODY.PEEK[HEADER.FIELDS ({' '.join(LEAN_HEADERS)})]"
_THREAD_PART = "BODY.PEEK[HEADER.FIELDS (IN-REPLY-TO MESSAGE-ID REFERENCES)]"

# The upper bound of each bucket, exclusive. The search takes these names, not bytes.
SIZE_BUCKETS = {
    "small": 100 * 1024,
    "medium": 1024 * 1024,
    "large": 10 * 1024 * 1024,
    "huge": None,
}
AUTO_SUBMITTED = frozenset({"auto-generated", "auto-notified", "auto-replied", "no"})
PRECEDENCE = frozenset({"auto_reply", "bulk", "first-class", "junk", "list", "normal"})
AUTH_METHODS = ("dkim", "dmarc", "spf")
AUTH_VERDICTS = frozenset(  # RFC 8601 section 2.7, RFC 7489 section 11.2
    {"fail", "neutral", "none", "pass", "permerror", "policy", "softfail", "temperror"}
)
MAX_HEADER = 8192  # a longer value than this is no sender and no verdict

SEARCH_SCAN_MAX = 1000  # UIDs one search looks through to fill its page
THREAD_WINDOW_DAYS = 90  # either side of the mail the thread starts from
THREAD_SCAN_MAX = 500  # mails whose ids are compared, the nearest UIDs first
THREAD_MAX = 50  # rows returned, the newest
MAX_THREAD_IDS = 50  # ids taken from one mail, the newest, so a long References is bounded

_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_LABEL = r"(?!-)[a-z0-9-]{1,63}(?<!-)"
_DOMAIN_RE = re.compile(rf"(?:{_LABEL}\.)+{_LABEL}", re.ASCII)
_METHOD_RE = re.compile(r"\s*([a-z0-9._-]+)\s*=\s*([a-z0-9_-]+)(?:\s|$)", re.ASCII)


# --- headers ---------------------------------------------------------------------------


def parse_headers(raw: bytes) -> Message:
    """A header block as a Message. Decoded first, so an internationalised address keeps
    its domain instead of becoming an opaque Header object."""
    return HeaderParser(policy=compat32).parsestr(raw.decode("utf-8", "replace"))


def normal_domain(value: str) -> str | None:
    """Lower-case ASCII with punycode for anything else, so a lookalike in another script
    shows as what it is; None unless the result is a plain host name."""
    try:
        ace = value.encode("idna").decode("ascii").lower()
    except UnicodeError:
        return None
    return ace if len(ace) <= 253 and _DOMAIN_RE.fullmatch(ace) else None


def _sender(msg: Message) -> str | None:
    """The one address of the one From header, its domain normalised, or None. Stays in
    the process: the keep list needs the local part, and the model never gets it."""
    froms = msg.get_all("from") or ()
    if len(froms) != 1 or len(froms[0]) > MAX_HEADER:
        return None
    found = getaddresses([froms[0]])
    if len(found) != 1 or not _BARE_ADDRESS_RE.fullmatch(found[0][1]):
        return None
    local, _, domain = found[0][1].rpartition("@")
    ascii_domain = normal_domain(domain)
    return f"{local}@{ascii_domain}".lower() if ascii_domain else None


def _word(value: str | None, allowed: frozenset[str]) -> str | None:
    """A closed word for a header's first token: None if absent, `other` if not listed."""
    if value is None:
        return None
    token = value.split(";")[0].strip().lower()
    return token if token in allowed else "other"


def _has_attachment(msg: Message) -> bool:
    """By the top-level Content-Type, with no MIME parser next to the credential: a
    multipart/mixed, or one part that is neither text nor multipart. An attachment nested
    below multipart/signed is not seen."""
    ctype = msg.get_content_type()
    return ctype == "multipart/mixed" or ctype.split("/")[0] not in ("multipart", "text")


def _size(n: int | None) -> str | None:
    if n is None:
        return None
    return next(name for name, top in SIZE_BUCKETS.items() if top is None or n < top)


def _without_comments(value: str) -> str | None:
    """RFC 8601 CFWS: comments and quoted strings removed, so a `;` or a `dkim=pass`
    inside them is not read. None when one of them does not end."""
    out: list[str] = []
    depth = 0
    quoted = escaped = False
    for ch in value:
        if escaped:
            escaped = False
        elif quoted or depth:
            if ch == "\\":
                escaped = True
            elif quoted:
                quoted = ch != '"'
            elif ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
        elif ch == '"':
            quoted = True
            out.append('""')
        elif ch == "(":
            depth = 1
            out.append(" ")
        else:
            out.append(ch)
    return None if quoted or depth or escaped else "".join(out)


class Lean:
    """The projection for one profile. A record carries no folder name and nothing a mail
    wrote: see the module docstring."""

    def __init__(
        self, *, keep: Iterable[str], authserv_id: str | None, suppress: Iterable[str] = ()
    ):
        self._keep = parse_keep(keep)
        self._authserv_id = authserv_id
        self._suppress = tuple(suppress)

    def _suppressed(self, sender: str | None) -> bool:
        """A listed domain and its subdomains, as the keep list matches: hiding too much
        is the safe direction. A sender that cannot be read is not suppressed, and its
        record names no domain."""
        domain = sender.rpartition("@")[2] if sender else ""
        return any(domain == d or domain.endswith("." + d) for d in self._suppress)

    def verdicts(self, msg: Message) -> dict | None:
        """DKIM, DMARC and SPF from the topmost Authentication-Results, and only when the
        receiving server wrote it. That server prepends, so a header the sender forged
        with the real authserv-id sits below its own. Without an authserv-id in the
        profile there is no verdict at all.

        A method the header does not name, or names with results that disagree, is
        `unknown`. A verdict is no authentication of the mail: that this provider
        removes forged headers from a mail it did not check is unmeasured."""
        headers = msg.get_all("authentication-results") or ()
        if self._authserv_id is None or not headers or len(headers[0]) > MAX_HEADER:
            return None
        text = _without_comments(headers[0].lower())
        if text is None:
            return None
        head, *results = text.split(";")
        if head.split()[:1] != [self._authserv_id.lower()]:
            return None
        seen: dict[str, set[str]] = {m: set() for m in AUTH_METHODS}
        for result in results:
            m = _METHOD_RE.match(result)
            if m and m.group(1) in seen:
                seen[m.group(1)].add(m.group(2))
        return {
            method: next(iter(found)) if len(found) == 1 and found <= AUTH_VERDICTS else "unknown"
            for method, found in seen.items()
        }

    def record(self, row: _Row) -> dict | None:
        """The lean record, or None for a mail from a suppressed domain."""
        msg = parse_headers(row.header)
        sender = _sender(msg)
        if self._suppressed(sender):
            return None
        unsubscribe = msg.get("list-unsubscribe") is not None
        categories = classify(
            from_addr=sender or "",
            subject="",  # never fetched: a bounce is told by its sender here
            headers={"list-unsubscribe": ""} if unsubscribe else {},
            size_bytes=row.size or 0,
        )
        return {
            "auth": self.verdicts(msg),
            "auto_submitted": _word(msg.get("auto-submitted"), AUTO_SUBMITTED),
            "categories": sorted(c.value for c in categories),
            "date": row.arrived.date().isoformat() if row.arrived else None,
            "has_attachment": _has_attachment(msg),
            "has_list_id": msg.get("list-id") is not None,
            "has_list_unsubscribe": unsubscribe,
            "keep_hit": _is_kept(sender, *self._keep) if sender else None,
            "precedence": _word(msg.get("precedence"), PRECEDENCE),
            "sender_domain": sender.rpartition("@")[2] if sender else None,
            "size": _size(row.size),
            "uid": row.uid,
        }

    def rows(self, mb, uids: Sequence[str]) -> list[dict]:
        """The records of `uids`, in that order. A UID with no mail and a mail from a
        suppressed domain are left out alike. The folder is selected by the caller."""
        found = {r.uid: r for r in _fetch(mb, list(uids), _LEAN_PART)}
        records = (self.record(found[u]) for u in uids if u in found)
        return [r for r in records if r is not None]

    def search(self, mb, criteria: str, limit: int) -> tuple[list[dict], bool]:
        """The newest `limit` records by UID, and whether more UIDs are left.

        The page fills from further back when a mail is suppressed: rows cut after the
        limit would show a short page with `truncated`, and that pair says a listed mail
        exists. The look back stops after SEARCH_SCAN_MAX UIDs."""
        found = sorted(_uid_list(_uid_cmd(mb, "SEARCH", criteria)), key=int, reverse=True)
        rows: list[dict] = []
        scanned = 0
        while len(rows) < limit and scanned < min(len(found), SEARCH_SCAN_MAX):
            batch = found[scanned : scanned + limit - len(rows)]
            rows += self.rows(mb, batch)
            scanned += len(batch)
        return rows, scanned < len(found)


# --- search ----------------------------------------------------------------------------


def imap_date(d: datetime.date) -> str:
    """RFC 3501 date. By table: strftime follows the locale."""
    return f"{d.day:02d}-{_MONTHS[d.month - 1]}-{d.year:04d}"


def _size_criteria(bucket: str) -> list[str]:
    names = list(SIZE_BUCKETS)
    top = SIZE_BUCKETS[bucket]
    i = names.index(bucket)
    # LARGER and SMALLER are strict: a bucket is low <= size < top
    parts = [f"LARGER {SIZE_BUCKETS[names[i - 1]] - 1}"] if i else []
    return parts + ([f"SMALLER {top}"] if top is not None else [])


def search_criteria(
    *,
    since: datetime.date | None = None,
    before: datetime.date | None = None,
    seen: bool | None = None,
    flagged: bool | None = None,
    answered: bool | None = None,
    size: str | None = None,
) -> str:
    """The IMAP criteria, from typed values only. SINCE and BEFORE compare the server's
    own arrival date, not the Date header a sender wrote. Allowed: date, flag, size."""
    parts: list[str] = []
    if since is not None:
        parts.append(f"SINCE {imap_date(since)}")
    if before is not None:
        parts.append(f"BEFORE {imap_date(before)}")
    for name, value in (("SEEN", seen), ("FLAGGED", flagged), ("ANSWERED", answered)):
        if value is not None:
            parts.append(name if value else f"UN{name}")
    if size is not None:
        parts.extend(_size_criteria(size))
    return " ".join(parts) or "ALL"


# --- thread ----------------------------------------------------------------------------


def _ids(row: _Row) -> frozenset[str]:
    """A mail's own id and the ids it names, newest last under the cap. They are compared
    here and nowhere else, so no id is cleaned: none reaches a command or a result."""
    msg = parse_headers(row.header)
    found: list[str] = []
    for name in ("references", "in-reply-to", "message-id"):
        for value in msg.get_all(name) or ():
            found += parse_message_ids(value)
    return frozenset(found[-MAX_THREAD_IDS:])


def thread_uids(mb, uid: str) -> tuple[list[str], bool]:
    """The UIDs around `uid`, ascending, and whether the answer was cut short.

    Mails join when they share an id, compared here. The server gets one date SEARCH and
    no Message-ID, so a hostile References header cannot fan out into commands or reach
    one. The price is a window: mails within THREAD_WINDOW_DAYS of the start, the
    THREAD_SCAN_MAX nearest UIDs. Only the selected folder is searched: Sent is outside
    what the bot reads.
    """
    start = next(iter(_fetch(mb, [uid], _THREAD_PART)), None)
    if start is None:
        return [], False
    if start.arrived is None:
        return [uid], False
    day = start.arrived.date()
    low = day - datetime.timedelta(days=THREAD_WINDOW_DAYS)
    high = day + datetime.timedelta(days=THREAD_WINDOW_DAYS + 1)  # BEFORE excludes its day
    found = _uid_list(_uid_cmd(mb, "SEARCH", f"SINCE {imap_date(low)} BEFORE {imap_date(high)}"))
    nearest = sorted(found, key=lambda u: abs(int(u) - int(uid)))[:THREAD_SCAN_MAX]
    ids_of = {r.uid: _ids(r) for r in _fetch(mb, nearest, _THREAD_PART)}
    members = {uid}
    wanted = set(_ids(start))
    changed = True
    while changed:
        changed = False
        for other, ids in ids_of.items():
            if other not in members and ids & wanted:
                members.add(other)
                wanted |= ids
                changed = True
    ordered = sorted(members, key=int)
    return ordered[-THREAD_MAX:], len(found) > len(nearest) or len(ordered) > THREAD_MAX
