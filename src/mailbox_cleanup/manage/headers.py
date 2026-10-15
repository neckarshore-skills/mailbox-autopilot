"""Header values for outgoing drafts: one definition, used by the reply builder
(`draft.py`) and the new-mail builder (`compose.py`).

Both builders write values that did not originate in this program — a reply takes them
from the mail being answered, a new mail from the command line — so both pass through
the same two checks before a header is set.
"""

from __future__ import annotations

import re
from email.headerregistry import Address
from email.message import EmailMessage

# EmailMessage() (default policy) raises ValueError on a raw CR/LF in a header value; a
# hostile Subject must still produce a draft. Control characters (C0 `\x00-\x1f`, DEL
# `\x7f`, and C1 `\x80-\x9f` — e.g. U+0085 NEL, which some decoders treat as a line
# break) and whitespace runs collapse to a single space before a header is set.
# To and Cc together, for a new mail and for a reply. A mail that deceived the agent into
# addressing a crowd is stopped here, not by a human counting recipients in a draft.
MAX_RECIPIENTS = 10

_CONTROL_OR_WS_RUN_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]+|\s+")

# One bare address, by allowlist. Local part: RFC 5322 atoms joined by single dots. Domain:
# letter-digit-hyphen labels joined by single dots. Nothing else: no quoted string, no
# comment `( )`, no group `: ;`, no domain literal `[ ]`, no backslash.
#
# It is an allowlist, not a list of forbidden characters, because the value is handed to
# the mail library's address parser, which reinterprets more than a denylist can name.
# Measured on Python 3.11 against the earlier denylist: `=?utf-8?q?x=40y.org=2C_b?=@z.org`
# has one `@`, no space and no comma, and was stored as TWO recipients.
_ATOM = r"[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+"
_LABEL = r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?"
STRICT_ADDR_RE = re.compile(rf"{_ATOM}(?:\.{_ATOM})*@{_LABEL}(?:\.{_LABEL})*")


def is_bare_address(value: str) -> bool:
    """True when `value` is exactly one plain ASCII address that the mail library will
    write as typed. `=?` is refused although `=` and `?` are legal atom characters: it
    opens an RFC 2047 encoded-word, which the parser decodes inside the local part."""
    return value.isascii() and "=?" not in value and bool(STRICT_ADDR_RE.fullmatch(value))


def clean_header_value(value: str) -> str:
    return _CONTROL_OR_WS_RUN_RE.sub(" ", value).strip()


class AddressHeaderError(ValueError):
    """The header did not store exactly the given bare addresses."""


def set_addresses(msg: EmailMessage, header: str, addrs: list[str]) -> None:
    """Write `addrs` as address objects, then read the header back and compare.

    Address objects, not a joined string: a string is parsed again by the mail library,
    and that second parse is where one accepted value turned into two recipients. The
    read-back is the second lock: whatever the header holds must be exactly the input,
    one address each and no display name, or AddressHeaderError is raised."""
    try:
        msg[header] = [Address(addr_spec=a) for a in addrs]
        stored = [(a.addr_spec, a.display_name) for a in msg[header].addresses]
    except Exception as e:  # the parser raises several unrelated types on odd input
        raise AddressHeaderError(header) from e
    if stored != [(a, "") for a in addrs]:
        raise AddressHeaderError(header)
