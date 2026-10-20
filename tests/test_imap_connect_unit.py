"""imap_connect retries only the connect/login step; a body exception propagates as-is
(CodeRabbit on #54). Before the fix it was caught by the retry loop, which slept, logged
in again and yielded a second time -- contextlib then raised RuntimeError, so the CLI's
PartialMoveError / PreviewMismatchError handlers never ran."""

import pytest

from mailbox_cleanup import imap_client
from mailbox_cleanup.auth import Credentials
from mailbox_cleanup.imap_client import IMAPConnectionError, imap_connect
from mailbox_cleanup.operations.batching import PartialMoveError

CREDS = Credentials(email="t@example.com", password="x", server="imap.example.com")


class _Box:
    logins = 0
    logouts = 0
    fail_logins = 0

    def __init__(self, server, port, **kwargs):  # kwargs: ssl_context since the TLS fix
        pass

    def login(self, email, password):
        type(self).logins += 1
        if type(self).logins <= type(self).fail_logins:
            raise OSError("connection reset")
        return self

    def logout(self):
        type(self).logouts += 1


@pytest.fixture
def box(monkeypatch):
    b = type("Box", (_Box,), {"logins": 0, "logouts": 0, "fail_logins": 0})
    monkeypatch.setattr(imap_client, "MailBox", b)
    monkeypatch.setattr(imap_client.time, "sleep", lambda s: None)
    return b


def test_body_exception_propagates_without_a_second_login(box):
    err = PartialMoveError(["1"], ["2"], TimeoutError())
    with pytest.raises(PartialMoveError) as exc:
        with imap_connect(CREDS, ssl=True):
            raise err
    assert exc.value is err
    assert box.logins == 1
    assert box.logouts == 1


def test_login_failure_is_retried_then_succeeds(box):
    box.fail_logins = 1
    with imap_connect(CREDS, ssl=True) as mb:
        assert mb is not None
    assert box.logins == 2
    assert box.logouts == 1


def test_login_failure_after_retries_is_a_connection_error(box):
    box.fail_logins = 99
    with pytest.raises(IMAPConnectionError):
        with imap_connect(CREDS, ssl=True, max_retries=2):
            pass
    assert box.logins == 3
