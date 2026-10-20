"""Shared test double: an IMAP client that announces MOVE and turns UID MOVE into a call
to the fake mailbox's own move(uids, target), so tests can keep asserting on it.

Since #79, batching sends UID MOVE through mb.client.uid itself instead of calling
imap_tools' MailBox.move().
"""

from types import SimpleNamespace


def _target(raw) -> str:
    t = raw.decode() if isinstance(raw, bytes) else str(raw)
    return t[1:-1] if len(t) >= 2 and t[0] == t[-1] == '"' else t


def move_capable_client(mb):
    def uid(command, uidset, *args):
        if command != "MOVE":
            raise AssertionError(f"unexpected UID {command} on a MOVE-capable server")
        mb.move(uidset.split(","), _target(args[0]))
        return ("OK", [b""])

    return SimpleNamespace(capabilities=("MOVE",), uid=uid)
