"""The bot's lean path against a real IMAP server (GreenMail), for what a fake cannot hold:
the wire format of FETCH with RFC822.SIZE and HEADER.FIELDS, EXAMINE, the server's own
evaluation of date, flag and size criteria, and that a read leaves a mail unseen.

Host-level guards are in tests/test_bot_lean.py. These drive `Lean` directly: the host
always asks for TLS, and GreenMail here serves plaintext on loopback."""

import datetime

import pytest

from mailbox_cleanup import lean as lean_mod
from mailbox_cleanup.auth import Credentials
from mailbox_cleanup.guards import FolderView
from mailbox_cleanup.imap_client import imap_connect
from mailbox_cleanup.lean import Lean, search_criteria, thread_uids

pytestmark = pytest.mark.integration

NEWSLETTER = (
    b"From: Anna Beispiel <anna@sender.example>\r\n"
    b"To: test@localhost\r\n"
    b"Subject: CANARY-SUBJECT\r\n"
    b"Date: Tue, 27 Oct 2026 08:00:00 +0100\r\n"
    b"Message-ID: <n1@sender.example>\r\n"
    b"List-Unsubscribe: <https://sender.example/u>\r\n"
    b"List-Id: <news.sender.example>\r\n"
    b"Auto-Submitted: auto-generated\r\n"
    b"Precedence: bulk\r\n"
    b"Authentication-Results: mx.example.org; dkim=fail\r\n"
    b"Authentication-Results: mx.example.org; dkim=pass\r\n"
    b"Content-Type: multipart/mixed; boundary=x\r\n"
    b"\r\n"
    b"--x\r\nContent-Type: text/plain\r\n\r\nCANARY-BODY\r\n--x--\r\n"
)
PLAIN = (
    b"From: boss@bank.example\r\n"
    b"To: test@localhost\r\n"
    b"Subject: Bitte\r\n"
    b"Date: Wed, 28 Oct 2026 08:00:00 +0100\r\n"
    b"Message-ID: <p1@bank.example>\r\n"
    b"In-Reply-To: <n1@sender.example>\r\n"
    b"\r\n" + b"x" * 200_000 + b"\r\n"
)
CLINIC = (
    b"From: arzt@klinik.example\r\n"
    b"To: test@localhost\r\n"
    b"Subject: Befund\r\n"
    b"Date: Thu, 29 Oct 2026 08:00:00 +0100\r\n"
    b"Message-ID: <k1@klinik.example>\r\n"
    b"\r\n"
    b"privat\r\n"
)


@pytest.fixture
def box(fresh_mailbox, seed_raw):
    """Three mails, opened the way the host opens them: no SELECT at login, then EXAMINE."""
    seed_raw(NEWSLETTER, PLAIN, CLINIC)
    g = fresh_mailbox
    creds = Credentials(email=g["user"], password=g["password"], server=g["host"])
    with imap_connect(creds, port=g["port"], ssl=False, max_retries=0, select_inbox=False) as mb:
        view = FolderView(mb.folder.list(), namespace="Bot")
        mb.folder.set(view.check_readable("inbox"), readonly=True)
        yield mb


LEAN = Lean(keep=("boss@bank.example",), authserv_id="mx.example.org", suppress=("klinik.example",))


def _by_domain(rows):
    return {r["sender_domain"]: r for r in rows}


def test_a_real_server_gives_the_lean_record(box):
    rows, truncated = LEAN.search(box, "ALL", 10)
    assert truncated is False
    assert sorted(_by_domain(rows)) == ["bank.example", "sender.example"]  # the clinic is not
    news = _by_domain(rows)["sender.example"]
    assert news["auth"] == {"dkim": "fail", "dmarc": "unknown", "spf": "unknown"}  # topmost
    assert news["auto_submitted"] == "auto-generated" and news["precedence"] == "bulk"
    assert news["has_attachment"] is True and news["has_list_id"] is True
    assert news["has_list_unsubscribe"] is True and news["categories"] == ["newsletter"]
    assert news["size"] == "small" and news["date"] is not None
    assert datetime.date.fromisoformat(news["date"])
    bank = _by_domain(rows)["bank.example"]
    assert bank["size"] == "medium" and bank["keep_hit"] is True and bank["auth"] is None


def test_no_header_of_a_real_mail_is_fetched_beyond_the_lean_list(box):
    uid = LEAN.search(box, "ALL", 10)[0][0]["uid"]
    data = box.client.uid("FETCH", uid, lean_mod._LEAN_PART.join("()"))
    assert b"CANARY" not in b"".join(x[1] for x in data[1] if isinstance(x, tuple))


@pytest.mark.parametrize(
    "criteria, domains",
    [
        (search_criteria(seen=False), ["bank.example", "sender.example"]),
        (search_criteria(seen=True), []),
        (search_criteria(flagged=True), []),
        (search_criteria(answered=False), ["bank.example", "sender.example"]),
        (search_criteria(size="small"), ["sender.example"]),
        (search_criteria(size="medium"), ["bank.example"]),
        (search_criteria(size="huge"), []),
    ],
    ids=str,
)
def test_a_real_server_evaluates_date_flag_and_size_criteria(box, criteria, domains):
    assert sorted(_by_domain(LEAN.search(box, criteria, 10)[0])) == domains


def test_a_real_server_evaluates_the_arrival_date(box):
    today = datetime.date.today()
    day = datetime.timedelta(days=2)
    since = search_criteria(since=today - day, before=today + day)
    assert len(LEAN.search(box, since, 10)[0]) == 2
    assert LEAN.search(box, search_criteria(before=today - day), 10)[0] == []


def test_reading_leaves_every_mail_unseen(box):
    rows, _ = LEAN.search(box, "ALL", 10)
    LEAN.rows(box, [r["uid"] for r in rows])
    assert len(LEAN.search(box, "UNSEEN", 10)[0]) == 2


def test_a_real_thread_is_found_without_a_header_search(box):
    rows, _ = LEAN.search(box, "ALL", 10)
    bank = _by_domain(rows)["bank.example"]["uid"]
    uids, truncated = thread_uids(box, bank)
    assert truncated is False
    assert len(uids) == 2 and bank in uids
