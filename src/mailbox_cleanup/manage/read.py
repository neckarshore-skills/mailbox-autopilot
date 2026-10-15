"""`manage read`: fetch one message by UID (spec §3 layer 3). Never marks \\Seen."""

from __future__ import annotations

import re
from dataclasses import dataclass
from html.parser import HTMLParser

from .attachments import list_attachments
from .ids import UID_RE

_MSGID_RE = re.compile(r"<[^<>\s]+>")
# `uid` reaches IMAP as raw criteria text (F1: `AND(uid=...)` fails on GreenMail, the bare
# string `UID <n>` is the working form) — a digits-only check is stricter than the general
# control-character guard, and catches it before the value is ever interpolated (R1).
_UID_RE = UID_RE  # the name the CLI imports; the rule lives in ids.py


@dataclass(frozen=True)
class Message:
    uid: str
    folder: str
    message_id: str
    in_reply_to: str
    references: tuple[str, ...]
    sender: str
    reply_to: str
    to: tuple[str, ...]
    subject: str
    date: str
    text: str
    # tuple of manage.attachments.Attachment; listed by `read`, fetched by `save-attachment`
    attachments: tuple = ()
    # Cc of the answered mail, bare addresses; read by `manage draft` for reply-all
    cc: tuple[str, ...] = ()


class _Text(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip += 1
        elif tag in ("br", "p", "div", "li", "tr"):
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def html_to_text(html: str) -> str:
    p = _Text()
    p.feed(html)
    # Without close(), HTMLParser holds back trailing text that looks like the start of an
    # unfinished character reference (e.g. "AT&T", "&amp" with no ";") in its internal
    # buffer — it never reaches handle_data, silently dropping the whole text, not just
    # the tail.
    p.close()
    text = "".join(p.parts)
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


def parse_message_ids(value: str) -> tuple[str, ...]:
    return tuple(_MSGID_RE.findall(value or ""))


def _header(msg, name: str) -> str:
    vals = (msg.headers or {}).get(name.lower(), ())
    return (vals[0] if vals else "").strip()


def to_message(msg, folder: str) -> Message:
    ids = parse_message_ids(_header(msg, "message-id"))
    irt = parse_message_ids(_header(msg, "in-reply-to"))
    text = msg.text or (html_to_text(msg.html) if msg.html else "")
    return Message(
        uid=msg.uid or "",
        folder=folder,
        message_id=ids[0] if ids else "",
        in_reply_to=irt[0] if irt else "",
        references=parse_message_ids(_header(msg, "references")),
        sender=msg.from_ or "",
        reply_to=(msg.reply_to[0] if msg.reply_to else ""),
        to=tuple(msg.to or ()),
        subject=msg.subject or "",
        date=msg.date.isoformat() if msg.date else "",
        text=text,
        attachments=list_attachments(msg),
        cc=tuple(msg.cc or ()),
    )


def read_message(mb, *, uid: str, folder: str = "INBOX") -> Message | None:
    """Fetch one message by UID, without marking it \\Seen.

    `uid` must be all-ASCII digits (R1): it reaches IMAP as raw criteria text via the
    `UID <n>` form (F1 — `AND(uid=...)` gets a BAD "Search command not supported" from
    GreenMail 2.1.0), so anything else raises before any IMAP call is made.
    """
    if not _UID_RE.fullmatch(uid):
        raise ValueError(f"uid must contain only ASCII digits, got {uid!r}")
    mb.folder.set(folder)
    msgs = list(mb.fetch(f"UID {uid}", mark_seen=False, limit=1))
    return to_message(msgs[0], folder) if msgs else None
