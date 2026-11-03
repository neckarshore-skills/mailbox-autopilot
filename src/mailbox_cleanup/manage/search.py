"""`manage search`: candidates only (UID, sender, subject, date), never bodies (spec §3)."""

from __future__ import annotations

import datetime
import re
from dataclasses import dataclass
from email.parser import BytesHeaderParser
from email.policy import compat32
from email.utils import parsedate_to_datetime

from imap_tools import AND, MailMessage

from .args import unsafe_arg_keys

_NO_DATE = datetime.datetime.min.replace(tzinfo=datetime.UTC)
_CHUNK = 500  # UIDs per FETCH; with range collapse every command line stays short
# The server's SORT preselects a window; we re-sort it with our key. The slack absorbs
# servers whose ties or precision differ from ours (GreenMail 2.1.0 treats every undated
# mail of one day as equal under SORT DATE).
_SORT_SLACK = 50
_KEY_PART = "BODY.PEEK[HEADER.FIELDS (DATE)]"
_HEAD_PART = "BODY.PEEK[HEADER]"
# Item names are anchored to "(" or whitespace so X-GM-UID cannot supply the UID, and
# quoted strings are removed before matching so a header value cannot either.
_UID_RE = re.compile(rb"(?:\(|\s)UID (\d+)")
_INTERNALDATE_RE = re.compile(rb'(?:\(|\s)INTERNALDATE "([^"]+)"')
_SIZE_RE = re.compile(rb"(?:\(|\s)RFC822\.SIZE (\d+)")
_QUOTED_RE = re.compile(rb'"(?:[^"\\]|\\.)*"')
_BODY_QUOTED_RE = re.compile(rb'(?:\(|\s)BODY\[[^\]]*\] ("(?:[^"\\]|\\.)*"|NIL)', re.IGNORECASE)
_BODY_ITEM_RE = re.compile(rb"(?:\(|\s)BODY\[", re.IGNORECASE)
_FETCH_START_RE = re.compile(rb"^\d+ \(")
_MONTHS = {
    m: i
    for i, m in enumerate(
        ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"),
        start=1,
    )
}


@dataclass(frozen=True)
class Candidate:
    uid: str
    sender: str
    subject: str
    date: str
    to: str = ""  # the To header's addresses, comma-separated


@dataclass(frozen=True)
class _Row:
    uid: str
    arrived: datetime.datetime | None  # INTERNALDATE
    header: bytes  # the fetched header block (Date only, or the full header)
    has_header: bool = True  # whether the server sent the BODY[...] item at all
    size: int | None = None  # RFC822.SIZE, when it was asked for


def uid_set(uids) -> str:
    """Collapse UIDs into an IMAP sequence set: 1,2,3,5,7,8 -> "1:3,5,7:8"."""
    nums = sorted({int(u) for u in uids})
    parts: list[str] = []
    start = prev = None
    for n in nums:
        if start is None:
            start = prev = n
        elif n == prev + 1:
            prev = n
        else:
            parts.append(f"{start}:{prev}" if prev != start else str(start))
            start = prev = n
    if start is not None:
        parts.append(f"{start}:{prev}" if prev != start else str(start))
    return ",".join(parts)


def _parse_internaldate(raw: bytes) -> datetime.datetime | None:
    """`25-Sep-2026 07:00:46 +0000` (day may be space-padded); locale-independent."""
    try:
        day, mon, rest = raw.decode("ascii").strip().split("-", 2)
        year, clock, zone = rest.split(" ")
        hh, mm, ss = (int(x) for x in clock.split(":"))
        sign = -1 if zone[0] == "-" else 1
        offset = datetime.timedelta(hours=int(zone[1:3]), minutes=int(zone[3:5])) * sign
        return datetime.datetime(
            int(year), _MONTHS[mon], int(day), hh, mm, ss, tzinfo=datetime.timezone(offset)
        )
    except (ValueError, KeyError, IndexError):
        return None


def _header_date(header: bytes) -> datetime.datetime | None:
    value = BytesHeaderParser(policy=compat32).parsebytes(header).get("Date")
    if not value:
        return None
    try:
        d = parsedate_to_datetime(str(value))
    except (TypeError, ValueError, IndexError):
        return None
    if d is None:
        return None
    return d if d.tzinfo else d.replace(tzinfo=datetime.UTC)


def _sort_date(row: _Row) -> datetime.datetime | None:
    """RFC 5256 SORT DATE: the Date header, else the arrival date (INTERNALDATE)."""
    return _header_date(row.header) or row.arrived


def _sort_key(row: _Row) -> tuple[datetime.datetime, int]:
    return (_sort_date(row) or _NO_DATE, int(row.uid))


def _has_sort(mb) -> bool:
    """Read capabilities AFTER login: some servers (Dovecot) advertise SORT only then."""
    typ, data = mb.client.capability()
    if typ != "OK":
        return False
    tokens = b" ".join(d for d in data if isinstance(d, bytes)).upper().split()
    return b"SORT" in tokens


def _uid_cmd(mb, command: str, *args) -> list:
    typ, data = mb.client.uid(command, *args)
    if typ != "OK":
        raise RuntimeError(f"IMAP UID {command} failed: {typ}")
    return data


def _uid_list(data) -> list[str]:
    return data[0].decode().split() if data and data[0] else []


def _records(data) -> list[tuple[bytes, bytes | None]]:
    """imaplib's FETCH data as (meta, literal) pairs; literal is None when absent."""
    records: list[list] = []
    for item in data:
        if isinstance(item, tuple):
            records.append([item[0], item[1] or b""])
        elif isinstance(item, bytes):
            if _FETCH_START_RE.match(item):
                records.append([item, None])  # a response without a literal
            elif records:
                records[-1][0] += item  # items after the literal, e.g. b" UID 5)"
    return [(meta, lit) for meta, lit in records]


def _parse_fetch(data, wanted: set[str]) -> list[_Row]:
    """Rows for the UIDs we asked for, one per UID.

    - Records for UIDs outside `wanted` are dropped: servers may send unsolicited FETCH
      responses (another client changing flags) during any command.
    - Several records for one UID are merged: for each item the FIRST non-empty value
      wins, so a later flags-only record cannot blank an earlier header.
    """
    merged: dict[str, dict] = {}
    for meta, literal in _records(data):
        header_item = None
        if literal is not None and _BODY_ITEM_RE.search(meta):
            header_item = literal
        else:
            quoted = _BODY_QUOTED_RE.search(meta)
            if quoted:
                value = quoted.group(1)
                header_item = b"" if value.upper() == b"NIL" else _unquote(value)
        bare = _QUOTED_RE.sub(b'""', meta)
        uid_m = _UID_RE.search(bare)
        if not uid_m or uid_m.group(1).decode() not in wanted:
            continue
        uid = uid_m.group(1).decode()
        idate = _INTERNALDATE_RE.search(meta)
        arrived = _parse_internaldate(idate.group(1)) if idate else None
        size_m = _SIZE_RE.search(bare)
        rec = merged.setdefault(uid, {"arrived": None, "header": None, "size": None})
        if rec["arrived"] is None:
            rec["arrived"] = arrived
        if rec["size"] is None and size_m:
            rec["size"] = int(size_m.group(1))
        if header_item is not None and not rec["header"]:
            rec["header"] = header_item
    return [
        _Row(
            uid=uid,
            arrived=rec["arrived"],
            header=rec["header"] or b"",
            has_header=rec["header"] is not None,
            size=rec["size"],
        )
        for uid, rec in merged.items()
    ]


def _unquote(value: bytes) -> bytes:
    return re.sub(rb"\\(.)", rb"\1", value[1:-1])


def _fetch(mb, uids: list[str], part: str) -> list[_Row]:
    rows: list[_Row] = []
    for i in range(0, len(uids), _CHUNK):
        chunk = uids[i : i + _CHUNK]
        data = _uid_cmd(mb, "FETCH", uid_set(chunk), f"(UID INTERNALDATE {part})")
        rows += _parse_fetch(data, set(chunk))
    return rows


def _criteria(sender, subject, text, since, recipient=None) -> tuple[str, list[tuple[str, str]]]:
    """Split filters into an ASCII criteria string and non-ASCII (key, value) pairs.

    Non-ASCII values go to the server as IMAP literals (RFC 3501 does not allow 8-bit
    data in quoted strings; GreenMail 2.1.0 silently matches nothing for them)."""
    ascii_crit: dict = {}
    literals: list[tuple[str, str]] = []
    for key, imap_key, value in (
        ("from_", "FROM", sender),
        ("to", "TO", recipient),
        ("subject", "SUBJECT", subject),
        ("text", "TEXT", text),
    ):
        if not value:
            continue
        if value.isascii():
            ascii_crit[key] = value
        else:
            literals.append((imap_key, value))
    if since:
        ascii_crit["sent_date_gte"] = since
    return (str(AND(**ascii_crit)) if ascii_crit else "ALL"), literals


def _uids_with_literals(mb, base: str, literals: list[tuple[str, str]]) -> list[str]:
    """One SEARCH per non-ASCII value (imaplib sends one literal per command); the
    result is the intersection, in ascending UID order."""
    found: set[str] | None = None
    for imap_key, value in literals:
        mb.client.literal = value.encode("utf-8")
        uids = set(_uid_list(_uid_cmd(mb, "SEARCH", "CHARSET", "UTF-8", f"{base} {imap_key}")))
        found = uids if found is None else found & uids
    return sorted(found or (), key=int)


def _top(rows: list[_Row], limit: int) -> list[_Row]:
    return sorted(rows, key=_sort_key, reverse=True)[:limit]


def search(
    mb,
    *,
    folder: str = "INBOX",
    sender: str | None = None,
    recipient: str | None = None,
    subject: str | None = None,
    text: str | None = None,
    since: datetime.date | None = None,
    limit: int = 20,
) -> list[Candidate]:
    """Candidates only — never bodies (spec §3 unit 1). Newest first.

    Ordering follows RFC 5256 SORT DATE on every path: the Date header, or the arrival
    date (INTERNALDATE) when Date is missing or unparseable; the higher UID wins a tie.
    With server SORT (all filters ASCII), SORT preselects `limit + slack` UIDs; otherwise
    only the Date header and INTERNALDATE of every match are fetched. Either way the
    window is ordered here with the same key, and full headers are fetched only for the
    top `limit`. `since` compares against the Date header (IMAP SENTSINCE). `recipient`
    matches the To header only (IMAP TO), never Cc, From or the body.

    Read-only by construction. The no-destructive guard
    (tests/test_manage_no_destructive.py) is a spelling check that catches mistakes,
    such as a copied destructive method call or IMAP command string; it is not a proof
    against intent, since a command assembled at runtime passes it. Review stays the
    gate."""
    bad = unsafe_arg_keys(
        folder=folder, sender=sender, recipient=recipient, subject=subject, text=text
    )
    if bad:
        raise ValueError(f"control characters in: {', '.join(bad)}")
    limit = max(limit, 0)
    mb.folder.set(folder)
    base, literals = _criteria(sender, subject, text, since, recipient)
    if not literals and _has_sort(mb):
        # SORT's criteria are ASCII only here: GreenMail's SORT cannot evaluate literals.
        uids = _uid_list(_uid_cmd(mb, "SORT", "(REVERSE DATE)", "UTF-8", base))
        uids = uids[: limit + _SORT_SLACK]
    elif literals:
        uids = _uids_with_literals(mb, base, literals)
    else:
        uids = _uid_list(_uid_cmd(mb, "SEARCH", "CHARSET", "UTF-8", base))
    top = _top(_fetch(mb, uids, _KEY_PART), limit)
    full = {r.uid: r for r in _fetch(mb, [r.uid for r in top], _HEAD_PART)}
    candidates = []
    for key_row in top:
        row = full.get(key_row.uid)
        if row is None or not row.has_header:  # expunged meanwhile, or header never sent
            continue
        msg = MailMessage.from_bytes(row.header)
        d = _sort_date(row)
        candidates.append(
            Candidate(
                uid=row.uid,
                sender=msg.from_ or "",
                subject=msg.subject or "",
                date=d.isoformat() if d else "",
                to=", ".join(msg.to),
            )
        )
    return candidates
