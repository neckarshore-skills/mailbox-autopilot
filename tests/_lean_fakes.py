"""Shared test double for the bot host: an IMAP server that knows only what the lean path
may ask for. Anything else raises, so a guard that lets a command through fails its test
even when the output happens to look clean. Every command lands in `log`, and every
header name a FETCH asked for lands in `fetched_fields`.

It answers in imaplib's shape. The real wire format is held by the GreenMail tests in
tests/integration/test_lean_read.py, which run in CI.
"""

import datetime
import re
from dataclasses import dataclass, field
from types import SimpleNamespace

_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_ITEM_RE = re.compile(
    r"\(UID INTERNALDATE(?: RFC822\.SIZE)? BODY\.PEEK\[HEADER\.FIELDS \(([A-Z0-9 -]+)\)\]\)"
)


class Forbidden(AssertionError):
    """The lean path sent something it must never send."""


def _imap_date(text: str) -> datetime.date:
    day, month, year = text.split("-")
    return datetime.date(int(year), _MONTHS.index(month) + 1, int(day))


@dataclass
class Mail:
    header: bytes
    arrived: str = "28-Oct-2026 07:00:46 +0000"
    size: int = 2048
    flags: tuple = ()
    body: bytes = b"CANARY-BODY\r\n"

    @property
    def day(self) -> datetime.date:
        return _imap_date(self.arrived.split(" ")[0])


def _uids(uidset: str) -> list[int]:
    out: list[int] = []
    for part in uidset.split(","):
        low, _, high = part.partition(":")
        out.extend(range(int(low), int(high or low) + 1))
    return out


def _fields(header: bytes, names: set[str]) -> bytes:
    """The named fields of a header block, in message order, ending in a blank line."""
    kept: list[bytes] = []
    keeping = False
    for line in header.split(b"\r\n"):
        if line[:1] in (b" ", b"\t"):
            if keeping:
                kept.append(line)
            continue
        keeping = line.split(b":", 1)[0].decode("latin-1").upper() in names
        if keeping:
            kept.append(line)
    return b"\r\n".join(kept) + b"\r\n\r\n"


@dataclass
class FakeImap:
    folders: dict
    listing: list = field(default_factory=list)
    log: list = field(default_factory=list)
    fetched_fields: set = field(default_factory=set)
    selected: str | None = None

    def __post_init__(self):
        self.listing = self.listing or [(name, "/", ()) for name in self.folders]
        self.client = SimpleNamespace(uid=self._uid, capabilities=("IMAP4REV1",))
        self.client.capability = lambda: ("OK", [b"IMAP4rev1"])
        self.folder = SimpleNamespace(list=self._list, set=self._set)

    # the commands -------------------------------------------------------------------

    def _list(self):
        self.log.append(("LIST",))
        return [SimpleNamespace(name=n, delim=d, flags=f) for n, d, f in self.listing]

    def _set(self, name, readonly=False):
        self.log.append(("EXAMINE" if readonly else "SELECT", name))
        if name not in self.folders:
            raise Forbidden(f"no such folder: {name}")
        self.selected = name

    def logout(self):
        self.log.append(("LOGOUT",))

    def _uid(self, command, *args):
        self.log.append(("UID", command, *args))
        if command == "SEARCH":
            return self._search(*args)
        if command == "FETCH":
            return self._fetch(*args)
        raise Forbidden(f"UID {command}")

    def _search(self, criteria):
        mails = self.folders[self.selected]
        hits = [u for u, m in sorted(mails.items()) if self._matches(m, criteria.split())]
        return ("OK", [" ".join(map(str, hits)).encode()])

    @staticmethod
    def _matches(m: Mail, tokens: list) -> bool:
        ok = True
        while tokens:
            t = tokens.pop(0)
            if t == "ALL":
                continue
            if t == "SINCE":
                ok &= m.day >= _imap_date(tokens.pop(0))
            elif t == "BEFORE":
                ok &= m.day < _imap_date(tokens.pop(0))
            elif t == "LARGER":
                ok &= m.size > int(tokens.pop(0))
            elif t == "SMALLER":
                ok &= m.size < int(tokens.pop(0))
            elif t in ("SEEN", "FLAGGED", "ANSWERED"):
                ok &= f"\\{t.title()}" in m.flags
            elif t in ("UNSEEN", "UNFLAGGED", "UNANSWERED"):
                ok &= f"\\{t[2:].title()}" not in m.flags
            else:
                raise Forbidden(f"search criterion {t}")
        return ok

    def _fetch(self, uidset, items):
        wanted = _ITEM_RE.fullmatch(items)
        if not wanted:
            raise Forbidden(f"fetch items {items}")
        names = set(wanted.group(1).split())
        self.fetched_fields |= names
        sized = "RFC822.SIZE" in items
        out: list = []
        mails = self.folders[self.selected]
        for uid in _uids(uidset):
            if uid not in mails:
                continue
            m = mails[uid]
            literal = _fields(m.header, names)
            size = f" RFC822.SIZE {m.size}" if sized else ""
            meta = f'1 (UID {uid} INTERNALDATE "{m.arrived}"{size} BODY[HEADER.FIELDS] '
            out.append(((meta + "{" + str(len(literal)) + "}").encode(), literal))
            out.append(b")")
        return ("OK", out)

    # what the tests read --------------------------------------------------------------

    def commands(self, verb: str) -> list:
        return [c for c in self.log if c[0] == verb or c[:2] == ("UID", verb)]
