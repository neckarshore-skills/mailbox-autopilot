"""`manage draft`: build an RFC 5322 reply and IMAP-APPEND it to Drafts (spec §7).

Nothing is ever sent — see tests/test_no_send.py, which forbids any send-mail call in
`src/`. A draft is appended with the \\Draft flag; the manage layer never creates a
Drafts folder on a guess (spec §7.2) and never deletes, moves or flags-as-deleted
(tests/test_manage_no_destructive.py).
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from email.message import EmailMessage
from email.utils import formatdate, make_msgid, parseaddr

from imap_tools import MailMessageFlags

from ..folders import resolve_folder
from .headers import MAX_RECIPIENTS, clean_header_value, is_bare_address, set_addresses
from .ids import safe_message_id
from .read import Message

_RE_PREFIX = re.compile(r"^\s*(re|aw|antw)\s*:", re.IGNORECASE)

# R1: cap the outgoing References chain at the tail of this many IDs (the most recent
# ancestors plus the original's own Message-ID, always last) — the same "keep the tail"
# shape as thread._MAX_IDS. A very long legitimate thread loses its oldest ancestors in
# the header, never its most recent ones — harmless for threading, which keys on
# In-Reply-To and the last IDs.
_MAX_REFERENCES = 20


class NoDraftsFolderError(Exception):
    """No \\Drafts folder was found. Spec §7.2: stop; never create one on a guess."""


_NO_SENDER = "original has no usable sender address"
_NO_RECIPIENT = f"{_NO_SENDER}; draft has no recipient"


class RecipientError(ValueError):
    """A refused recipient plan. `code` is the CLI error code; the message names the rule,
    never an address: it ends up in output next to mail-derived text."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass
class Recipients:
    to: list[str] = field(default_factory=list)
    cc: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _bare(value: str) -> str | None:
    """The one bare address in `value`, or None. Same rule as everywhere else: parseaddr,
    then the strict allowlist (headers.is_bare_address) on what it returned."""
    _, addr = parseaddr(value or "")
    return addr if addr and is_bare_address(addr) else None


def plan_recipients(
    original: Message,
    *,
    own_addr: str,
    sender_only: bool = False,
    drop: Sequence[str] = (),
) -> Recipients:
    """Who gets the reply (design docs/2026-10-15-reply-all-design.md).

    Every address comes from the answered mail, which its sender controls; the caller can
    only take addresses away. Main recipient: Reply-To, else From. Reply-all (default)
    adds the original To to To and keeps the original Cc in Cc. The owner's own address is
    removed and duplicates collapse, both case-insensitively. On a mail the owner sent,
    the reply goes to that mail's To instead of back to the owner. Unusable addresses are
    left out with a count. More than MAX_RECIPIENTS is refused, never truncated.
    """
    own = own_addr.lower()
    plan = Recipients()
    seen = {own}
    unusable = 0

    def add(target: list[str], raw: str) -> None:
        nonlocal unusable
        if not raw:
            return
        addr = _bare(raw)
        if addr is None:
            unusable += 1
        elif addr.lower() not in seen:
            seen.add(addr.lower())
            target.append(addr)

    main = _bare(original.reply_to or original.sender)
    self_sent = main is not None and main.lower() == own
    if main is not None and not self_sent:
        seen.add(main.lower())
        plan.to.append(main)
    if self_sent or not sender_only:
        for raw in original.to:
            add(plan.to, raw)
    if not sender_only:
        for raw in original.cc:
            add(plan.cc, raw)

    wanted = []
    for value in drop:
        addr = _bare(value)
        if addr is None or addr.lower() not in {a.lower() for a in plan.to + plan.cc}:
            raise RecipientError(
                "not_a_recipient",
                "--drop takes one address that is among the planned recipients; "
                "run --preview to see them",
            )
        wanted.append(addr.lower())
    plan.to = [a for a in plan.to if a.lower() not in wanted]
    plan.cc = [a for a in plan.cc if a.lower() not in wanted]
    if not plan.to and plan.cc:
        plan.to.append(plan.cc.pop(0))

    if len(plan.to) + len(plan.cc) > MAX_RECIPIENTS:
        raise RecipientError(
            "too_many_recipients",
            f"the reply would go to more than {MAX_RECIPIENTS} addresses; answer with "
            "--sender-only, or leave addresses out with --drop",
        )

    if main is None:
        plan.warnings.append(_NO_RECIPIENT if not (plan.to or plan.cc) else _NO_SENDER)
    if unusable:
        noun = "address" if unusable == 1 else "addresses"
        verb = "is" if unusable == 1 else "are"
        plan.warnings.append(
            f"{unusable} recipient {noun} of the original {verb} unusable and left out"
        )
    return plan


def build_reply(
    original: Message, *, from_addr: str, body: str, recipients: Recipients | None = None
) -> tuple[EmailMessage, list[str]]:
    """Build an in-memory RFC 5322 reply. Never touches the network.

    Threading (In-Reply-To/References) is attempted only when `original.message_id` is
    present and passes the same strict allowlist used before a Message-ID reaches IMAP
    as a search value (R1 of the Task 7 dispatch ruling) — a mail's own References
    header is likewise untrusted, so each entry is checked the same way before it is
    copied into the outgoing header, and the chain is capped to its most recent
    `_MAX_REFERENCES` ids. The returned warnings are for the human, not the audit log.
    """
    warnings: list[str] = []
    msg = EmailMessage()

    subject = clean_header_value(original.subject or "")
    msg["Subject"] = subject if _RE_PREFIX.match(subject) else f"Re: {subject}".strip()
    msg["From"] = from_addr

    # Recipients are bare addresses that passed the strict allowlist (plan_recipients);
    # they are written as address objects and read back (headers.set_addresses), the same
    # two locks `manage compose` uses. A display name is untrusted, mail-derived text and
    # never written. The "was it one address?" history of this check: see git log.
    plan = recipients if recipients is not None else plan_recipients(original, own_addr=from_addr)
    warnings.extend(plan.warnings)
    if plan.to:
        set_addresses(msg, "To", plan.to)
    if plan.cc:
        set_addresses(msg, "Cc", plan.cc)

    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=from_addr.split("@")[-1] or None)

    mid = original.message_id
    if not mid:
        warnings.append("original has no Message-ID; draft is not threaded")
    elif safe_message_id(mid) != mid:
        warnings.append("original Message-ID is malformed; draft is not threaded")
    else:
        msg["In-Reply-To"] = mid
        clean_refs = [r for r in original.references if safe_message_id(r) == r]
        dropped = len(original.references) - len(clean_refs)
        if dropped:
            suffix = "y" if dropped == 1 else "ies"
            warnings.append(f"dropped {dropped} malformed References entr{suffix}")
        chain = [r for r in clean_refs if r != mid]
        full_chain = [*chain, mid]
        if len(full_chain) > _MAX_REFERENCES:
            full_chain = full_chain[-_MAX_REFERENCES:]
            warnings.append(f"References chain trimmed to the last {_MAX_REFERENCES} IDs")
        msg["References"] = " ".join(full_chain)

    msg.set_content(body)
    return msg, warnings


def save_draft(mb, msg: EmailMessage) -> str:
    """IMAP-APPEND `msg` into the resolved Drafts folder with the \\Draft flag.

    Raises NoDraftsFolderError instead of creating one (spec §7.2) — never appends
    anything when no Drafts folder is found.
    """
    folder = resolve_folder(mb, "drafts")
    if folder is None:
        raise NoDraftsFolderError("No \\Drafts folder found; create it in your mail client")
    mb.append(msg.as_bytes(), folder, flag_set=[MailMessageFlags.DRAFT])
    return folder
