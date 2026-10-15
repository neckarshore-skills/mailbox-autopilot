"""`manage compose`: build a NEW RFC 5322 mail for the Drafts folder (compose design,
docs/2026-10-08-compose-design.md).

Nothing is ever sent; the message built here is handed to `draft.save_draft`, which
appends it to Drafts. Unlike a reply, a new mail has no original to take the recipient
from: every address is caller input. So the address rule is stricter than the reply's —
a value that is not exactly one bare ASCII address is refused instead of dropped with a
warning, because a draft that silently lost a recipient reads like a finished one.
"""

from __future__ import annotations

from collections.abc import Sequence
from email.message import EmailMessage
from email.utils import formatdate, make_msgid

from .headers import (
    MAX_RECIPIENTS,
    AddressHeaderError,
    clean_header_value,
    is_bare_address,
    set_addresses,
)


class ComposeError(ValueError):
    """A refused header value. The message names the option and the rule, never the
    value: it ends up in the command's output next to mail-derived text."""


def _bad_address(option: str) -> ComposeError:
    return ComposeError(
        f"{option} takes one bare ASCII address per use (name@example.org), "
        "without a display name; repeat the option for more recipients"
    )


def _check_addresses(option: str, values: Sequence[str]) -> list[str]:
    for value in values:
        if not is_bare_address(value):
            raise _bad_address(option)
    return list(values)


def _set_addresses(msg: EmailMessage, header: str, option: str, addrs: list[str]) -> None:
    try:
        set_addresses(msg, header, addrs)
    except AddressHeaderError as e:
        raise _bad_address(option) from e


def build_new(
    *,
    from_addr: str,
    to: Sequence[str],
    cc: Sequence[str] = (),
    subject: str,
    body: str,
) -> EmailMessage:
    """Build an in-memory RFC 5322 mail with To, Cc and Subject. Never touches the network.

    No Bcc, no attachment and no threading header is ever set: this builder has no
    parameter for them (design §1, non-goals 2 to 4).
    """
    to_addrs = _check_addresses("--to", to)
    cc_addrs = _check_addresses("--cc", cc)
    if not to_addrs:
        raise ComposeError("--to is required: a draft needs at least one recipient")
    if len(to_addrs) + len(cc_addrs) > MAX_RECIPIENTS:
        raise ComposeError(f"--to and --cc together take at most {MAX_RECIPIENTS} addresses")
    clean_subject = clean_header_value(subject)
    if not clean_subject:
        raise ComposeError("--subject is required and must not be blank")

    msg = EmailMessage()
    msg["Subject"] = clean_subject
    msg["From"] = from_addr
    _set_addresses(msg, "To", "--to", to_addrs)
    if cc_addrs:
        _set_addresses(msg, "Cc", "--cc", cc_addrs)
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=from_addr.split("@")[-1] or None)
    msg.set_content(body)
    return msg
