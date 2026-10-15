"""`manage draft`: append an RFC 5322 reply to Drafts with the \\Draft flag (Task 7).
See tests/test_manage_draft_unit.py for the "no Drafts folder" case (R6: no Docker
needed for it) and for build_reply's pure-function threading/sanitization behaviour."""

import json
import smtplib
import time

import pytest
from imap_tools import AND

from mailbox_cleanup.folders import resolve_folder
from mailbox_cleanup.manage.draft import build_reply, save_draft
from mailbox_cleanup.manage.read import read_message
from mailbox_cleanup.manage.search import search

pytestmark = pytest.mark.integration


def _seed(
    message_id: str,
    subject: str,
    body: str = "Hallo",
    addresses: str = "From: mira@example.org\r\nTo: test@localhost\r\n",
) -> None:
    s = smtplib.SMTP("127.0.0.1", 3025, local_hostname="localhost")  # see tests/conftest.py
    s.sendmail(
        "seed@example.com",
        ["test@localhost"],
        (f"{addresses}Subject: {subject}\r\nMessage-ID: {message_id}\r\n\r\n{body}\r\n").encode(),
    )
    s.quit()
    time.sleep(0.5)


def _uid_by_message_id(mb, sender, message_id, folder="INBOX"):
    """R6: never pick a candidate by `[0]` — filter search() hits down to the ONE whose
    own Message-ID header is the exact id this test seeded."""
    hits = search(mb, sender=sender)
    matches = [
        h.uid for h in hits if read_message(mb, uid=h.uid, folder=folder).message_id == message_id
    ]
    assert len(matches) == 1, [h.uid for h in hits]
    return matches[0]


def _find_draft_by_message_id(mb, folder, message_id):
    """R6: find the draft by ITS OWN Message-ID, not `(d,) = fetch(all)` — the drafts
    folder is a container reused across tests (F3) and may already hold other drafts."""
    mb.folder.set(folder)
    hits = [
        d
        for d in mb.fetch(AND(all=True), mark_seen=False)
        if d.headers.get("message-id", ("",))[0].strip() == message_id
    ]
    assert len(hits) == 1
    return hits[0]


def _assert_not_seen(mb, folder, uid):
    mb.folder.set(folder)
    (after,) = list(mb.fetch(f"UID {uid}", mark_seen=False, limit=1))
    assert "\\Seen" not in after.flags


def _delete_folder_if_present(mb, name):
    try:
        mb.folder.delete(name)
    except Exception:  # noqa: BLE001 — "no such folder" is the expected common case
        pass


def test_draft_lands_in_drafts_with_flag_and_threading(drafts_ready, open_mb):
    _seed("<q1@example.org>", "Frage")
    with open_mb(drafts_ready) as mb:
        uid = _uid_by_message_id(mb, "mira@example.org", "<q1@example.org>")
        orig = read_message(mb, uid=uid)
        msg, warnings = build_reply(orig, from_addr="test@localhost", body="Antwort")
        assert warnings == []
        folder = save_draft(mb, msg)
        assert folder == resolve_folder(mb, "drafts")
        d = _find_draft_by_message_id(mb, folder, msg["Message-ID"])
        assert "\\Draft" in d.flags
        assert d.headers["in-reply-to"][0].strip() == "<q1@example.org>"
        assert d.subject == "Re: Frage"
        _assert_not_seen(mb, "INBOX", uid)


def test_cli_draft_writes_and_reports_folder(drafts_ready, patch_account, tmp_path, open_mb):
    from click.testing import CliRunner

    from mailbox_cleanup.cli import cli

    _seed("<t1@example.org>", "Termin", body="Passt Dienstag?")
    patch_account(drafts_ready)
    with open_mb(drafts_ready) as mb:
        uid = _uid_by_message_id(mb, "mira@example.org", "<t1@example.org>")
    body = tmp_path / "body.txt"
    body.write_text("Dienstag passt.", encoding="utf-8")
    res = CliRunner().invoke(
        cli, ["manage", "draft", "--no-quote", "--uid", uid, "--body-file", str(body)]
    )
    assert res.exit_code == 0, res.output
    out = json.loads(res.output)
    assert out["ok"] is True and out["subcommand"] == "manage.draft"
    assert out["drafts_folder"] and out["warnings"] == []
    assert out["subject"].startswith("<mail-content>\n") and "Re: Termin" in out["subject"]
    (to,) = out["to"]  # the seeded mail went to the account's own address only
    assert to.startswith("<mail-content>\n") and "mira@example.org" in to
    assert out["cc"] == []
    with open_mb(drafts_ready) as mb:
        _assert_not_seen(mb, "INBOX", uid)


def test_cli_draft_answers_everyone_on_a_real_server(
    drafts_ready, patch_account, tmp_path, open_mb
):
    """Reply-all end to end: Cc is parsed from a real IMAP fetch, the own address leaves,
    the preview matches the write, and the appended draft carries To and Cc as planned
    (docs/2026-10-15-reply-all-design.md)."""
    from click.testing import CliRunner

    from mailbox_cleanup.cli import cli

    _seed(
        "<ra1@example.org>",
        "Runde",
        body="Wer kommt?",
        addresses=(
            "From: Mira <mira@example.org>\r\n"
            "To: test@localhost, Ben <ben@example.org>\r\n"
            "Cc: cara@example.net, TEST@localhost\r\n"
        ),
    )
    patch_account(drafts_ready)
    with open_mb(drafts_ready) as mb:
        uid = _uid_by_message_id(mb, "mira@example.org", "<ra1@example.org>")
    body = tmp_path / "body.txt"
    body.write_text("Ich komme.", encoding="utf-8")
    res = CliRunner().invoke(cli, ["manage", "draft", "--uid", uid, "--preview"])
    assert res.exit_code == 0, res.output
    preview = json.loads(res.output)
    res = CliRunner().invoke(
        cli, ["manage", "draft", "--quote-lang", "de", "--uid", uid, "--body-file", str(body)]
    )
    assert res.exit_code == 0, res.output
    out = json.loads(res.output)
    assert out["to"] == preview["to"] and out["cc"] == preview["cc"]
    assert len(out["to"]) == 2
    assert "mira@example.org" in out["to"][0] and "ben@example.org" in out["to"][1]
    (cc,) = out["cc"]
    assert "cara@example.net" in cc
    with open_mb(drafts_ready) as mb:
        mb.folder.set(out["drafts_folder"])
        drafts = [
            d
            for d in mb.fetch(AND(all=True), mark_seen=False)
            if d.headers.get("in-reply-to", ("",))[0].strip() == "<ra1@example.org>"
        ]
        assert len(drafts) == 1
        assert drafts[0].to == ("mira@example.org", "ben@example.org")
        assert drafts[0].cc == ("cara@example.net",)
        text = drafts[0].text.replace("\r\n", "\n")
        assert text.startswith("Ich komme.\n\n")
        # The seeded mail may carry no Date header; the line then reads "<addr> schrieb:".
        attribution, quoted = text.split("\n\n", 1)[1].split("\n", 1)
        assert "mira@example.org" in attribution and "schrieb" in attribution
        assert "1900" not in attribution
        assert quoted.startswith("> Wer kommt?")


def test_save_draft_to_a_non_ascii_folder_name(manage_mailbox, open_mb, monkeypatch):
    """R5 measurement: whether GreenMail/imap_tools mangles a non-ASCII folder name on
    APPEND was unmeasured before this test. Result (see PR body / report): `save_draft`
    round-trips correctly through "Entwürfe" as-is — no modified-UTF-7 encoding needed
    on this stack (GreenMail 2.1.0 + imap_tools), the draft is appended and found again
    under the exact folder name."""
    import mailbox_cleanup.manage.draft as draft_mod

    with open_mb(manage_mailbox) as mb:
        _delete_folder_if_present(mb, "Entwürfe")
        mb.folder.create("Entwürfe")
        try:
            # A pre-existing "Drafts" in the reused container would otherwise win normal
            # resolution (folders.DRAFTS_FALLBACKS lists "Drafts" before "Entwürfe") — force
            # resolve_folder's answer here so this test targets "Entwürfe" specifically.
            monkeypatch.setattr(draft_mod, "resolve_folder", lambda mb, kind: "Entwürfe")
            uid = _uid_by_message_id(mb, "mira@example.org", "<r1@example.org>")
            orig = read_message(mb, uid=uid)
            msg, _ = build_reply(orig, from_addr="test@localhost", body="x")
            folder = save_draft(mb, msg)
            assert folder == "Entwürfe"
            d = _find_draft_by_message_id(mb, "Entwürfe", msg["Message-ID"])
            assert "\\Draft" in d.flags
        finally:
            _delete_folder_if_present(mb, "Entwürfe")
