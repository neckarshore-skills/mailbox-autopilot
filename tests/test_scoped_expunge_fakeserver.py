"""#73 end to end without Docker: a fake IMAP server that, like Hostinger, announces MOVE
and UIDPLUS only AFTER login (James #79, finding 8).

Greenmail announces its capabilities before login, so it cannot see this split. Here the
real imap_connect, the real imaplib and the real move_in_batches talk to a socket.
UID 4 is marked \\Deleted by "another client" and must survive our move of UIDs 1 and 2.
"""

import re
import socket
import threading

import pytest

from mailbox_cleanup.auth import Credentials
from mailbox_cleanup.imap_client import imap_connect
from mailbox_cleanup.operations.batching import (
    MoveUnsupportedError,
    PartialMoveError,
    move_in_batches,
)

PRE_LOGIN = "IMAP4rev1 AUTH=PLAIN"
DELETED = "\\Deleted"


class FakeIMAP:
    """Just enough IMAP4rev1 for login, SELECT and the move commands. One connection."""

    def __init__(self, post_login_caps: str, refuse: frozenset[str] = frozenset()):
        self.post = post_login_caps
        self.refuse = refuse  # UID subcommands answered with NO and not executed
        self.folders = {"INBOX": {1: set(), 2: set(), 3: set(), 4: {DELETED}}, "Trash": {}}
        self.selected = None
        self.after_select: list[str] = []
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(1)
        self.sock.settimeout(5)
        self.port = self.sock.getsockname()[1]
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _uids(self, spec: str) -> list[int]:
        out = []
        for part in spec.split(","):
            if ":" in part:
                a, b = part.split(":")
                hi = max(self.folders[self.selected] or [0]) if b == "*" else int(b)
                out.extend(range(int(a), hi + 1))
            else:
                out.append(int(part))
        return [u for u in out if u in self.folders[self.selected]]

    def _handle(self, tag: str, cmd: str, rest: str, logged_in: bool) -> list[str]:
        if cmd == "CAPABILITY":
            return [f"* CAPABILITY {self.post if logged_in else PRE_LOGIN}", f"{tag} OK done"]
        if cmd == "LOGIN":
            return [f"{tag} OK LOGIN completed"]
        if cmd in ("SELECT", "EXAMINE"):
            self.selected = rest.strip().strip('"')
            n = len(self.folders[self.selected])
            return [
                f"* {n} EXISTS",
                "* 0 RECENT",
                f"* FLAGS ({DELETED})",
                f"{tag} OK [READ-WRITE] SELECT completed",
            ]
        if cmd == "LOGOUT":
            return ["* BYE", f"{tag} OK LOGOUT completed"]
        if cmd == "EXPUNGE":
            self.after_select.append("EXPUNGE")
            box = self.folders[self.selected]
            for u in [u for u, f in box.items() if DELETED in f]:
                del box[u]
            return [f"{tag} OK EXPUNGE completed"]
        if cmd == "UID":
            sub, _, args = rest.partition(" ")
            sub = sub.upper()
            self.after_select.append(f"UID {sub}")
            box = self.folders[self.selected]
            caps = self.post.upper().split()
            if sub in self.refuse:
                return [f"{tag} NO [CANNOT] {sub} refused by test"]
            if sub == "MOVE" and "MOVE" in caps:
                spec, target = args.split(" ", 1)
                target = target.strip('"')
                for u in self._uids(spec):
                    self.folders[target][u] = box.pop(u)
                return [f"{tag} OK MOVE completed"]
            if sub == "COPY":
                spec, target = args.split(" ", 1)
                target = target.strip('"')
                for u in self._uids(spec):
                    self.folders[target][u] = set(box[u])
                return [f"{tag} OK COPY completed"]
            if sub == "STORE":
                spec = args.split(" ", 1)[0]
                for u in self._uids(spec):
                    box[u].add(DELETED)
                return [f"{tag} OK STORE completed"]
            if sub == "EXPUNGE" and "UIDPLUS" in caps:
                for u in self._uids(args.strip()):
                    if DELETED in box[u]:
                        del box[u]
                return [f"{tag} OK UID EXPUNGE completed"]
            if sub in ("SEARCH", "FETCH"):
                return [f"{tag} OK done"]
            return [f"{tag} BAD unsupported UID {sub}"]
        if cmd == "NOOP":
            return [f"{tag} OK NOOP"]
        return [f"{tag} BAD unknown {cmd}"]

    def _serve(self):
        try:
            conn, _ = self.sock.accept()
        except OSError:
            return
        conn.settimeout(5)
        f = conn.makefile("rwb")
        f.write(b"* OK fake IMAP ready\r\n")
        f.flush()
        logged_in = False
        try:
            for raw in f:
                line = raw.decode().rstrip("\r\n")
                m = re.match(r"(\S+) (\S+) ?(.*)", line)
                if not m:
                    continue
                tag, cmd, rest = m.group(1), m.group(2).upper(), m.group(3)
                if cmd == "SELECT":
                    self.after_select = []
                for out in self._handle(tag, cmd, rest, logged_in):
                    f.write(out.encode() + b"\r\n")
                f.flush()
                if cmd == "LOGIN":
                    logged_in = True
                if cmd == "LOGOUT":
                    return
        except OSError:
            return
        finally:
            conn.close()
            self.sock.close()


def _connect(server: FakeIMAP, monkeypatch):
    monkeypatch.setenv("MAILBOX_CLEANUP_SSL", "0")
    creds = Credentials(email="t@example.com", password="x", server="127.0.0.1")
    return imap_connect(creds, port=server.port, max_retries=0)


@pytest.mark.parametrize(
    "post_login",
    ["IMAP4rev1 MOVE UIDPLUS", "IMAP4rev1 UIDPLUS", "imap4rev1 move uidplus"],
    ids=["hostinger-move", "uidplus-only", "lowercase"],
)
def test_a_foreign_deleted_message_survives_our_move(monkeypatch, post_login):
    server = FakeIMAP(post_login)
    with _connect(server, monkeypatch) as mb:
        assert move_in_batches(mb, ["1", "2"], "Trash") == ["1", "2"]
    server.thread.join(5)
    inbox, trash = server.folders["INBOX"], server.folders["Trash"]
    assert 4 in inbox, "a plain EXPUNGE removed a message outside the selection"
    assert 3 in inbox
    assert {1, 2} <= set(trash) and not {1, 2} & set(inbox)
    assert "EXPUNGE" not in server.after_select  # never the unscoped form


def test_neither_move_nor_uidplus_sends_nothing_after_select(monkeypatch):
    server = FakeIMAP("IMAP4rev1")
    with pytest.raises(MoveUnsupportedError):
        with _connect(server, monkeypatch) as mb:
            move_in_batches(mb, ["1", "2"], "Trash")
    server.thread.join(5)
    assert server.after_select == []
    assert set(server.folders["INBOX"]) == {1, 2, 3, 4}
    assert server.folders["Trash"] == {}


@pytest.mark.parametrize(
    ("post_login", "refused"),
    [
        ("IMAP4rev1 MOVE UIDPLUS", "MOVE"),
        ("IMAP4rev1 UIDPLUS", "COPY"),
        ("IMAP4rev1 UIDPLUS", "STORE"),
    ],
    ids=["move-NO", "copy-NO", "store-NO"],
)
def test_a_NO_answer_stops_the_batch_before_anything_is_removed(monkeypatch, post_login, refused):
    """James on b0ffe03: an unchecked NO to COPY would still run STORE + UID EXPUNGE --
    UIDs gone, Trash empty, reported as ok. Every step's result is checked."""
    server = FakeIMAP(post_login, refuse=frozenset({refused}))
    with pytest.raises(PartialMoveError) as exc:
        with _connect(server, monkeypatch) as mb:
            move_in_batches(mb, ["1", "2"], "Trash")
    server.thread.join(5)
    assert exc.value.moved == [] and exc.value.remaining == ["1", "2"]
    inbox = server.folders["INBOX"]
    assert {1, 2, 3, 4} <= set(inbox), "a refused step still removed messages"
    assert "UID EXPUNGE" not in server.after_select
    assert "EXPUNGE" not in server.after_select
