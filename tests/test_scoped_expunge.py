"""#73: a move may remove only the UIDs it was given.

imap_tools' client-side move (no MOVE capability) is COPY + STORE \\Deleted + a plain,
unscoped EXPUNGE. A plain EXPUNGE removes EVERY message in the folder that carries
\\Deleted -- including ones another mail client only marked, which no preview showed.
imap_tools also decides on MOVE from the PRE-login capability list; Hostinger announces
MOVE and UIDPLUS only after login, so every move there took the unscoped path.
"""

from types import SimpleNamespace

import pytest

from mailbox_cleanup import imap_client
from mailbox_cleanup.auth import Credentials
from mailbox_cleanup.operations.batching import MoveUnsupportedError, move_in_batches


class CapMailbox:
    """Records every IMAP-level call; never runs a plain EXPUNGE silently."""

    def __init__(self, caps):
        self.calls = []
        self.client = SimpleNamespace(
            capabilities=tuple(caps), uid=self._uid, expunge=self._expunge
        )

    def _uid(self, command, *args):
        self.calls.append(("uid", command, *args))
        return ("OK", [b""])

    def _expunge(self):
        self.calls.append(("plain-expunge",))
        return ("OK", [b""])

    def move(self, uids, target):
        self.calls.append(("move", list(uids), target))

    def copy(self, uids, target):
        self.calls.append(("copy", list(uids), target))

    def expunge(self):
        self._expunge()

    def delete(self, uids):
        self.calls.append(("delete", list(uids)))


def test_native_move_when_the_server_has_move():
    mb = CapMailbox(["IMAP4REV1", "MOVE", "UIDPLUS"])
    move_in_batches(mb, ["3", "4"], "Trash")
    assert mb.calls == [("move", ["3", "4"], "Trash")]


def test_without_move_only_our_uids_are_expunged():
    mb = CapMailbox(["IMAP4REV1", "UIDPLUS"])
    moved = move_in_batches(mb, ["3", "4"], "Archive")
    assert moved == ["3", "4"]
    assert ("copy", ["3", "4"], "Archive") in mb.calls
    assert ("uid", "STORE", "3,4", "+FLAGS.SILENT", r"(\Deleted)") in mb.calls
    assert ("uid", "EXPUNGE", "3,4") in mb.calls
    assert ("plain-expunge",) not in mb.calls
    assert not any(c[0] in ("move", "delete") for c in mb.calls)


def test_without_move_and_uidplus_nothing_is_touched():
    mb = CapMailbox(["IMAP4REV1"])
    with pytest.raises(MoveUnsupportedError):
        move_in_batches(mb, ["3", "4"], "Trash")
    assert mb.calls == []


def test_capabilities_are_matched_case_insensitively_and_as_bytes():
    mb = CapMailbox([b"imap4rev1", b"uidplus"])
    move_in_batches(mb, ["9"], "Archive")
    assert ("uid", "EXPUNGE", "9") in mb.calls


# --- the capability list is refreshed after login ----------------------------------


def test_imap_connect_refreshes_capabilities_after_login(monkeypatch):
    class _Client:
        capabilities = ("IMAP4REV1", "AUTH=PLAIN")  # what the server sent before login

        def capability(self):
            return ("OK", [b"IMAP4rev1 MOVE UIDPLUS IDLE"])

    class _Box:
        def __init__(self, server, port, **kwargs):
            self.client = _Client()

        def login(self, email, password):
            return self

        def logout(self):
            pass

    monkeypatch.setattr(imap_client, "MailBox", _Box)
    monkeypatch.delenv("MAILBOX_CLEANUP_SSL", raising=False)
    creds = Credentials(email="t@example.com", password="x", server="imap.example.com")
    with imap_client.imap_connect(creds) as mb:
        assert "MOVE" in mb.client.capabilities
        assert "UIDPLUS" in mb.client.capabilities


def test_a_failed_capability_refresh_keeps_the_old_list(monkeypatch):
    class _Client:
        capabilities = ("IMAP4REV1",)

        def capability(self):
            raise OSError("connection reset")

    class _Box:
        def __init__(self, server, port, **kwargs):
            self.client = _Client()

        def login(self, email, password):
            return self

        def logout(self):
            pass

    monkeypatch.setattr(imap_client, "MailBox", _Box)
    monkeypatch.delenv("MAILBOX_CLEANUP_SSL", raising=False)
    creds = Credentials(email="t@example.com", password="x", server="imap.example.com")
    with imap_client.imap_connect(creds) as mb:
        assert mb.client.capabilities == ("IMAP4REV1",)  # batching then refuses safely
