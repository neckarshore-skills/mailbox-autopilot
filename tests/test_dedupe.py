from datetime import UTC, datetime
from unittest.mock import MagicMock

from mailbox_cleanup.operations.dedupe import run_dedupe


def _msg(uid, mid, date):
    m = MagicMock()
    m.uid = uid
    m.headers = {"message-id": (mid,)}
    m.date = date
    m.from_ = "x@y.com"
    m.subject = "s"
    return m


def test_dedupe_keeps_oldest_drops_rest(monkeypatch):
    mb = MagicMock()
    mb.client.capabilities = ("MOVE",)  # #73: batching reads the capability list
    mb.fetch.return_value = [
        _msg("1", "<dup@x>", datetime(2025, 1, 1, tzinfo=UTC)),
        _msg("2", "<dup@x>", datetime(2025, 1, 2, tzinfo=UTC)),
        _msg("3", "<dup@x>", datetime(2025, 1, 3, tzinfo=UTC)),
        _msg("4", "<unique@x>", datetime(2025, 1, 4, tzinfo=UTC)),
    ]
    trash = MagicMock()
    trash.name = "Papierkorb"
    trash.flags = ()
    inbox = MagicMock()
    inbox.name = "INBOX"
    inbox.flags = ()
    mb.folder.list.return_value = [inbox, trash]

    res = run_dedupe(mb, folder="INBOX", apply=True)
    assert sorted(res.duplicate_uids) == ["2", "3"]
    mb.move.assert_called_once()
    moved_uids, target = mb.move.call_args.args
    assert sorted(moved_uids) == ["2", "3"]
    assert target == "Papierkorb"


def test_dedupe_dry_run_does_not_move():
    mb = MagicMock()
    mb.client.capabilities = ("MOVE",)  # #73: batching reads the capability list
    mb.fetch.return_value = [
        _msg("1", "<dup@x>", datetime(2025, 1, 1, tzinfo=UTC)),
        _msg("2", "<dup@x>", datetime(2025, 1, 2, tzinfo=UTC)),
    ]
    trash = MagicMock()
    trash.name = "Trash"
    trash.flags = ()
    mb.folder.list.return_value = [trash]
    res = run_dedupe(mb, folder="INBOX", apply=False)
    mb.move.assert_not_called()
    assert res.dry_run is True
    assert res.duplicate_uids == ["2"]
