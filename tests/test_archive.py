from datetime import UTC, datetime
from unittest.mock import MagicMock

from mailbox_cleanup.operations.archive import run_archive
from tests._imap_fakes import move_capable_client


def _msg(uid, year):
    m = MagicMock()
    m.uid = uid
    m.from_ = "x@y.com"
    m.subject = f"msg {uid}"
    m.date = datetime(year, 6, 1, tzinfo=UTC)
    return m


def test_archive_groups_by_year_and_moves():
    mb = MagicMock()
    mb.client = move_capable_client(mb)  # #73/#79: UID MOVE goes through the client
    mb.fetch.return_value = [_msg("1", 2023), _msg("2", 2023), _msg("3", 2024)]
    # SPECIAL-USE detection: pretend Archive exists as plain folder
    archive_folder = MagicMock()
    archive_folder.name = "Archive"
    archive_folder.flags = ()
    inbox = MagicMock()
    inbox.name = "INBOX"
    inbox.flags = ()
    mb.folder.list.return_value = [inbox, archive_folder]
    mb.folder.exists.return_value = False  # subfolder needs creation

    res = run_archive(
        mb,
        folder="INBOX",
        older_than="12m",
        apply=True,
        now=datetime(2026, 5, 4, tzinfo=UTC),
    )

    # Check folders created and moves issued
    created = [c.args[0] for c in mb.folder.create.call_args_list]
    assert "Archive/2023" in created
    assert "Archive/2024" in created

    move_calls = mb.move.call_args_list
    moved_targets = sorted(c.args[1] for c in move_calls)
    assert moved_targets == ["Archive/2023", "Archive/2024"]
    assert res.dry_run is False
    assert sum(len(g["uids"]) for g in res.groups) == 3


def test_archive_dry_run_does_not_create_or_move():
    mb = MagicMock()
    mb.client = move_capable_client(mb)  # #73/#79: UID MOVE goes through the client
    mb.fetch.return_value = [_msg("1", 2023)]
    archive_folder = MagicMock()
    archive_folder.name = "Archive"
    archive_folder.flags = ()
    mb.folder.list.return_value = [archive_folder]
    res = run_archive(
        mb,
        folder="INBOX",
        older_than="12m",
        apply=False,
        now=datetime(2026, 5, 4, tzinfo=UTC),
    )
    mb.folder.create.assert_not_called()
    mb.move.assert_not_called()
    assert res.dry_run is True
    assert res.groups[0]["target"] == "Archive/2023"
