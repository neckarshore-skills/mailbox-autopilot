"""The bot's lean view of a mailbox (#76 section 4, slice 3c-2).

Everything here serves one promise: what a mail wrote reaches the model only as a word
from a closed list or a value of a strict shape, so no header can carry an instruction.
Three things hold it:

- **Only the headers this module needs are fetched.** Subject, To, Received and the rest
  never enter the process, so no later bug can return them. Nothing here fetches a body.
- **Every value is reduced, never echoed.** A header either becomes a boolean, a word
  from a closed list, one normalised domain, or nothing.
- **No server search takes a value from the model or from a mail.** The only criteria
  are a date, flags and size buckets, built here from typed values. `FROM` would be an
  oracle for a field that is never returned, because it matches the display name, and
  byte-level `LARGER` would be one for the exact size.

Nothing here knows the profile or the host. The caller passes the keep list, the
authserv-id and the suppressed domains, and validates its arguments before it gets here.
"""

from __future__ import annotations

import datetime
import functools
import re
from collections.abc import Iterable, Sequence
from email.message import Message
from email.parser import HeaderParser
from email.policy import compat32
from email.utils import getaddresses
from importlib import resources

from .classify import classify
from .guards import _BARE_ADDRESS_RE
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
INVALID_SENDER = "invalid"  # no host name has one label, so this cannot be a domain
SUFFIX_FILE = "public_suffix.dat"  # package data, root-owned with the package (L3)

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
    shows as what it is; None unless the result is a plain host name. The last label must
    hold a letter, so a numeric host such as an IP address is none (no IP leaves the host).

    This is Python's IDNA 2003 codec: it maps `ß` to `ss`. A sender's domain can only be
    misread into a neighbour by that; a list entry is refused in `profile` instead."""
    try:
        ace = value.encode("idna").decode("ascii").lower()
    except UnicodeError:
        return None
    if len(ace) > 253 or not _DOMAIN_RE.fullmatch(ace):
        return None
    return ace if any(c.isalpha() for c in ace.rpartition(".")[2]) else None


@functools.lru_cache(maxsize=1)
def load_suffix_rules() -> tuple[frozenset[str], frozenset[str], frozenset[str]]:
    """The vendored Public Suffix List: plain rules, wildcard parents, exceptions. The host
    reads a file that came with the package and goes to no network. It ages, and a suffix
    it does not know yet costs digest quality only, so it is bumped with a release."""
    text = resources.files(__package__).joinpath(SUFFIX_FILE).read_text(encoding="ascii")
    plain, wild, exceptions = set(), set(), set()
    for line in text.splitlines():
        if line.startswith("!"):
            exceptions.add(line[1:])
        elif line.startswith("*."):
            wild.add(line[2:])
        elif line and not line.startswith("//"):
            plain.add(line)
    return frozenset(plain), frozenset(wild), frozenset(exceptions)


def registrable_domain(domain: str) -> str:
    """The registrable domain of a normalised domain, by the Public Suffix List algorithm:
    the longest matching rule, an exception first, and otherwise the last label is the
    suffix. Everything left of it goes, so one label of what a sender wrote reaches the
    model, and not a tenant's or a person's name in a subdomain. The input of a domain
    that is itself a suffix comes back as it is."""
    plain, wild, exceptions = load_suffix_rules()
    labels = domain.split(".")
    suffix = 1
    for i in range(len(labels)):
        candidate = ".".join(labels[i:])
        if candidate in exceptions:
            suffix = len(labels) - i - 1
            break
        if candidate in plain or ".".join(labels[i + 1 :]) in wild:
            suffix = len(labels) - i
            break
    return ".".join(labels[-(suffix + 1) :])


def _forms(domain: str) -> set[str]:
    """A listed domain as it can stand in a raw From: its ASCII and its Unicode form."""
    try:
        return {domain, domain.encode("ascii").decode("idna")}
    except UnicodeError:
        return {domain}


def _ascii_address(address: str) -> str:
    local, _, domain = address.rpartition("@")
    return f"{local}@{normal_domain(domain) or domain}"


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
        addrs, domains = parse_keep(keep)  # compared with a sender, so in the sender's form
        self._keep = (
            frozenset(_ascii_address(a) for a in addrs),
            frozenset(normal_domain(d) or d for d in domains),
        )
        self._authserv_id = authserv_id
        self._suppress = tuple(suppress)
        self._suppress_names = tuple(sorted({n for d in self._suppress for n in _forms(d)}))
        load_suffix_rules()  # a host that cannot cut a domain does not start

    def _suppressed(self, sender: str | None, msg: Message) -> bool:
        """A listed domain and its subdomains, compared on the full domain, as the keep
        list matches: hiding too much is the safe direction.

        A From the parser cannot read is searched as text for a listed name, because the
        condition says the host returns nothing. An unreadable From of anybody else stays
        visible: hiding those would hand any sender a way out of the digest. What stays is
        an address hidden in an encoded word, and that record names no domain."""
        if sender:
            domain = sender.rpartition("@")[2]
            return any(domain == d or domain.endswith("." + d) for d in self._suppress)
        raw = " ".join(msg.get_all("from") or ()).lower()
        return any(name in raw for name in self._suppress_names)

    def verdicts(self, msg: Message) -> dict:
        """DKIM, DMARC and SPF from the topmost Authentication-Results, and only when the
        receiving server wrote it. That server prepends, so a header the sender forged
        with the real authserv-id sits below its own. Without an authserv-id in the
        profile there is no verdict at all.

        Anything else is `unknown`, never a pass: no such header, another authserv-id, a
        method the header does not name or names with results that disagree. A verdict is
        no authentication of the mail: that this provider removes forged headers from a
        mail it did not check is unmeasured."""
        seen: dict[str, set[str]] = {m: set() for m in AUTH_METHODS}
        headers = msg.get_all("authentication-results") or ()
        if self._authserv_id is None or not headers or len(headers[0]) > MAX_HEADER:
            return {m: "unknown" for m in AUTH_METHODS}
        text = _without_comments(headers[0].lower())
        head, *results = (text or "").split(";")
        if head.split()[:1] != [self._authserv_id.lower()]:
            return {m: "unknown" for m in AUTH_METHODS}
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
        if self._suppressed(sender, msg):
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
            "sender_domain": (
                registrable_domain(sender.rpartition("@")[2]) if sender else INVALID_SENDER
            ),
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
        """The newest `limit` records by UID, and whether there is more to return.

        Both parts of the answer count returned records, never UIDs, so neither can give a
        suppressed mail away: the page fills from further back, and `truncated` needs one
        record past the page, not one UID. The look back stops after SEARCH_SCAN_MAX UIDs,
        and only then does `truncated` mean that some were not looked at."""
        found = sorted(_uid_list(_uid_cmd(mb, "SEARCH", criteria)), key=int, reverse=True)
        rows: list[dict] = []
        scanned = 0
        while len(rows) <= limit and scanned < min(len(found), SEARCH_SCAN_MAX):
            batch = found[scanned : scanned + limit + 1 - len(rows)]
            rows += self.rows(mb, batch)
            scanned += len(batch)
        return rows[:limit], len(rows) > limit or scanned < len(found)


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
    unseen: bool | None = None,
    flagged: bool | None = None,
    size: str | None = None,
) -> str:
    """The IMAP criteria, from typed values only. SINCE compares the server's own arrival
    date, not the Date header a sender wrote."""
    parts: list[str] = []
    if since is not None:
        parts.append(f"SINCE {imap_date(since)}")
    if unseen is not None:
        parts.append("UNSEEN" if unseen else "SEEN")
    if flagged is not None:
        parts.append("FLAGGED" if flagged else "UNFLAGGED")
    if size is not None:
        parts.extend(_size_criteria(size))
    return " ".join(parts) or "ALL"
