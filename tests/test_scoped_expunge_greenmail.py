"""#73 against a real IMAP server: a message that another client only marked \\Deleted
survives our move, on the native MOVE path and on the UIDPLUS fallback path."""

import pytest

from mailbox_cleanup.auth import Credentials
from mailbox_cleanup.imap_client import imap_connect
from mailbox_cleanup.operations.batching import capabilities, move_in_batches

pytestmark = pytest.mark.integration

TARGET = "Scoped73"


def _creds(g):
    return Credentials(email=g["user"], password=g["password"], server=g["host"])


def _inbox_uids(mb):
    mb.folder.set("INBOX")
    typ, data = mb.client.uid("SEARCH", "ALL")
    assert typ == "OK"
    return data[0].decode().split()


@pytest.mark.parametrize("path", ["MOVE", "UIDPLUS"])
def test_a_foreign_deleted_mark_survives_our_move(seeded_mailbox, path):
    g = seeded_mailbox
    with imap_connect(_creds(g), port=g["port"]) as mb:
        caps = capabilities(mb)
        if path not in caps:
            pytest.skip(f"server does not offer {path}")
        if path == "UIDPLUS":
            # force the fallback branch: pretend the server has no MOVE
            mb.client.capabilities = tuple(c for c in mb.client.capabilities if c != "MOVE")
        if not any(f.name == TARGET for f in mb.folder.list()):
            mb.folder.create(TARGET)

        uids = _inbox_uids(mb)
        assert len(uids) == 4
        ours, untouched, foreign = uids[:2], uids[2], uids[3]
        # another mail client marked this one for deletion but has not expunged yet
        assert mb.client.uid("STORE", foreign, "+FLAGS", r"(\Deleted)")[0] == "OK"

        mb.folder.set("INBOX")
        assert move_in_batches(mb, ours, TARGET) == ours

        left = _inbox_uids(mb)
        assert untouched in left
        assert foreign in left, "a plain EXPUNGE removed a message outside the selection"
        assert not set(ours) & set(left)
        mb.folder.set(TARGET)
        typ, data = mb.client.uid("SEARCH", "ALL")
        assert len(data[0].split()) >= 2
        mb.folder.set("INBOX")
        mb.client.uid("STORE", foreign, "-FLAGS", r"(\Deleted)")
