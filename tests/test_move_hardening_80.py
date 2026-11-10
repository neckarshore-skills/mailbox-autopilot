"""#80 items 1-4 (James's review of #79): "nothing touched" and "scoped" literally true.

1. A refused archive creates no folder.
2. A failed CAPABILITY refresh is named in the refusal, not blamed on the server.
3. UIDPLUS path: STORE \\Deleted succeeded, UID EXPUNGE failed -> the flag is rolled back
   on the batch and the error says the copies stay in the target.
4. A UID set holds digits only; `*` or a range never reaches the server.
"""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from mailbox_cleanup import imap_client
from mailbox_cleanup.operations.archive import run_archive
from mailbox_cleanup.operations.batching import (
    MoveUnsupportedError,
    PartialMoveError,
    move_in_batches,
)


class Recorder:
    """An IMAP client that records UID commands; `fail` names a command that answers NO."""

    def __init__(self, caps, fail=None):
        self.calls = []
        self.fail = fail
        self.client = SimpleNamespace(capabilities=tuple(caps), uid=self._uid)

    def _uid(self, command, *args):
        self.calls.append((command, *args))
        if command == self.fail:
            return ("NO", [b"server says no"])
        return ("OK", [b""])


# --- item 1 -------------------------------------------------------------------------


def _old_msg(uid):
    m = MagicMock()
    m.uid = uid
    m.date = datetime(2020, 6, 1, tzinfo=UTC)
    return m


def test_a_refused_archive_creates_no_folder():
    mb = MagicMock()
    mb.client = SimpleNamespace(capabilities=("IMAP4REV1",), uid=MagicMock())
    mb.fetch.return_value = [_old_msg("1")]
    mb.folder.list.return_value = []
    mb.folder.exists.return_value = False
    with pytest.raises(MoveUnsupportedError):
        run_archive(
            mb, folder="INBOX", older_than="12m", apply=True, now=datetime(2026, 5, 4, tzinfo=UTC)
        )
    mb.folder.create.assert_not_called()
    mb.client.uid.assert_not_called()


def test_a_dry_run_archive_on_such_a_server_still_previews():
    mb = MagicMock()
    mb.client = SimpleNamespace(capabilities=("IMAP4REV1",), uid=MagicMock())
    mb.fetch.return_value = [_old_msg("1")]
    mb.folder.list.return_value = []
    res = run_archive(
        mb, folder="INBOX", older_than="12m", apply=False, now=datetime(2026, 5, 4, tzinfo=UTC)
    )
    assert res.groups[0]["count"] == 1


# --- item 2 -------------------------------------------------------------------------


def test_a_failed_capability_refresh_is_named_in_the_refusal():
    def boom():
        raise OSError("connection reset")

    mb = SimpleNamespace(client=SimpleNamespace(capabilities=("IMAP4REV1",), capability=boom))
    imap_client._refresh_capabilities(mb)
    with pytest.raises(MoveUnsupportedError) as e:
        move_in_batches(mb, ["3"], "Trash")
    msg = str(e.value)
    assert "capability" in msg.lower() and "refresh" in msg.lower()
    assert "OSError" in msg


def test_a_refresh_answered_no_is_named_too():
    mb = SimpleNamespace(
        client=SimpleNamespace(capabilities=("IMAP4REV1",), capability=lambda: ("NO", [b"later"]))
    )
    imap_client._refresh_capabilities(mb)
    with pytest.raises(MoveUnsupportedError) as e:
        move_in_batches(mb, ["3"], "Trash")
    assert "refresh" in str(e.value).lower()


def test_a_server_without_move_after_a_good_refresh_keeps_the_plain_reason():
    mb = SimpleNamespace(
        client=SimpleNamespace(
            capabilities=("IMAP4REV1",), capability=lambda: ("OK", [b"IMAP4REV1 IDLE"])
        )
    )
    imap_client._refresh_capabilities(mb)
    with pytest.raises(MoveUnsupportedError) as e:
        move_in_batches(mb, ["3"], "Trash")
    assert "refresh" not in str(e.value).lower()


# --- item 3 -------------------------------------------------------------------------


def test_a_failed_expunge_rolls_the_deleted_flag_back():
    mb = Recorder(["IMAP4REV1", "UIDPLUS"], fail="EXPUNGE")
    with pytest.raises(PartialMoveError) as e:
        move_in_batches(mb, ["3", "4"], "Archive")
    assert ("STORE", "3,4", "-FLAGS.SILENT", r"(\Deleted)") in mb.calls
    assert mb.calls.index(("EXPUNGE", "3,4")) < mb.calls.index(
        ("STORE", "3,4", "-FLAGS.SILENT", r"(\Deleted)")
    )
    assert e.value.moved == []
    assert "copies" in str(e.value.cause).lower() and "Archive" in str(e.value.cause)


def test_a_failed_rollback_is_reported_not_hidden():
    class Twice(Recorder):
        def _uid(self, command, *args):
            self.calls.append((command, *args))
            if command == "EXPUNGE" or (command == "STORE" and args[1] == "-FLAGS.SILENT"):
                return ("NO", [b"no"])
            return ("OK", [b""])

    mb = Twice(["IMAP4REV1", "UIDPLUS"])
    with pytest.raises(PartialMoveError) as e:
        move_in_batches(mb, ["3"], "Archive")
    assert "still marked" in str(e.value.cause).lower()


def test_a_failed_copy_sends_no_store_at_all():
    mb = Recorder(["IMAP4REV1", "UIDPLUS"], fail="COPY")
    with pytest.raises(PartialMoveError):
        move_in_batches(mb, ["3"], "Archive")
    assert [c[0] for c in mb.calls] == ["COPY"]


# --- item 4 -------------------------------------------------------------------------


@pytest.mark.parametrize("bad", ["*", "1:*", "2:5", "3,4", " 5", "", "x"])
def test_a_uid_that_is_not_digits_never_reaches_the_server(bad):
    mb = Recorder(["IMAP4REV1", "MOVE"])
    with pytest.raises(ValueError):
        move_in_batches(mb, ["1", bad], "Trash")
    assert mb.calls == []


def test_int_uids_are_refused_as_well():
    mb = Recorder(["IMAP4REV1", "MOVE"])
    with pytest.raises(ValueError):
        move_in_batches(mb, [1], "Trash")
    assert mb.calls == []
