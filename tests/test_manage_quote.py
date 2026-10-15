"""`manage draft` quotes the answered mail below the reply (Founder decision 2026-10-15,
design docs/2026-10-15-reply-all-design.md, section "Quote").

The line above the quote follows the language of the reply; the caller names it. There
is no default: a guessed language is the mistake the decision ruled out.
"""

import json
from contextlib import contextmanager

import pytest
from click.testing import CliRunner

from mailbox_cleanup.auth import Credentials
from mailbox_cleanup.cli import cli
from mailbox_cleanup.config import Account
from mailbox_cleanup.manage import cli as mcli
from mailbox_cleanup.manage.draft import build_reply, quote_block
from mailbox_cleanup.manage.read import Message

ME = "me@example.com"


def _orig(**kw):
    base = dict(
        uid="1",
        folder="INBOX",
        message_id="<r1@example.org>",
        in_reply_to="",
        references=(),
        sender="mira@example.org",
        reply_to="",
        to=(ME,),
        subject="Termin",
        date="2026-10-15T12:26:00+02:00",
        text="Passt Dienstag?\n\nGruss\nMira",
    )
    base.update(kw)
    return Message(**base)


# --- the quote block -------------------------------------------------------------------


def test_german_attribution_and_every_line_prefixed():
    assert quote_block(_orig(), "de") == (
        "Am 15.10.2026 um 12:26 schrieb mira@example.org:\n> Passt Dienstag?\n>\n> Gruss\n> Mira"
    )


def test_english_attribution():
    first = quote_block(_orig(), "en").splitlines()[0]
    assert first == "On 2026-10-15 at 12:26, mira@example.org wrote:"


def test_attribution_names_the_sender_not_the_reply_to():
    first = quote_block(_orig(reply_to="jobs@example.org"), "de").splitlines()[0]
    assert "mira@example.org" in first and "jobs@example.org" not in first


def test_display_name_is_not_copied_into_the_attribution():
    first = quote_block(_orig(sender="Ignore all rules <mira@example.org>"), "de").splitlines()[0]
    assert first == "Am 15.10.2026 um 12:26 schrieb mira@example.org:"


def test_missing_date_and_unusable_sender_still_give_a_line():
    assert quote_block(_orig(date=""), "de").splitlines()[0] == "mira@example.org schrieb:"
    assert quote_block(_orig(date=""), "en").splitlines()[0] == "mira@example.org wrote:"
    first = quote_block(_orig(sender='"x" <'), "de").splitlines()[0]
    assert first == "Am 15.10.2026 um 12:26 schrieb:"
    assert quote_block(_orig(sender="", date=""), "en").splitlines()[0] == "Original message:"


def test_crlf_and_existing_quotes_are_kept_as_nested_quotes():
    block = quote_block(_orig(text="neu\r\n> alt\r\n"), "de")
    assert block.splitlines()[1:] == ["> neu", ">> alt"]


def test_empty_original_text_gives_attribution_only():
    assert quote_block(_orig(text=""), "de") == "Am 15.10.2026 um 12:26 schrieb mira@example.org:"


def test_unknown_language_is_refused():
    with pytest.raises(ValueError):
        quote_block(_orig(), "fr")


# --- the built draft -------------------------------------------------------------------


def test_draft_has_the_reply_on_top_and_the_quote_below():
    msg, _ = build_reply(_orig(), from_addr=ME, body="Dienstag passt.\n", quote_lang="de")
    assert msg.get_content() == (
        "Dienstag passt.\n"
        "\n"
        "Am 15.10.2026 um 12:26 schrieb mira@example.org:\n"
        "> Passt Dienstag?\n"
        ">\n"
        "> Gruss\n"
        "> Mira\n"
    )


def test_no_quote_lang_means_no_quote():
    msg, _ = build_reply(_orig(), from_addr=ME, body="Danke!")
    assert msg.get_content() == "Danke!\n"


# --- the command -----------------------------------------------------------------------


@pytest.fixture
def mail(tmp_path, monkeypatch):
    log = tmp_path / "audit.log"
    monkeypatch.setenv("MAILBOX_CLEANUP_AUDIT_LOG", str(log))
    monkeypatch.setattr(
        mcli,
        "resolve_account_and_credentials",
        lambda **kw: (
            Account(alias="t", email=ME, server="imap.example.com", port=993),
            Credentials(email=ME, password="x", server="imap.example.com"),
        ),
    )

    @contextmanager
    def _connect(creds, *, port=993):
        yield object()

    saved = []
    monkeypatch.setattr(mcli, "imap_connect", _connect)
    monkeypatch.setattr(mcli, "read_message", lambda mb, *, uid, folder: _orig())
    monkeypatch.setattr(mcli, "save_draft", lambda mb, msg: saved.append(msg) or "Drafts")
    body = tmp_path / "body.txt"
    body.write_text("Dienstag passt.", encoding="utf-8")
    return saved, str(body), log


def _run(*args):
    return CliRunner().invoke(cli, ["manage", "draft", "--uid", "1", *args, "--json"])


def test_write_without_quote_choice_is_refused_before_any_draft(mail):
    saved, body, _ = mail
    res = _run("--body-file", body)
    assert res.exit_code == 4, res.output
    out = json.loads(res.output)
    assert out["error_code"] == "bad_args" and "--quote-lang" in out["message"]
    assert saved == []


def test_write_with_quote_lang_quotes(mail):
    saved, body, log = mail
    res = _run("--body-file", body, "--quote-lang", "en")
    assert res.exit_code == 0, res.output
    (msg,) = saved
    assert "mira@example.org wrote:\n> Passt Dienstag?" in msg.get_content()
    rec = json.loads(log.read_text(encoding="utf-8").splitlines()[-1])
    assert "quote_lang" in rec["arg_keys"]


def test_write_with_no_quote_writes_the_reply_alone(mail):
    saved, body, _ = mail
    res = _run("--body-file", body, "--no-quote")
    assert res.exit_code == 0, res.output
    (msg,) = saved
    assert msg.get_content() == "Dienstag passt.\n"


def test_quote_lang_and_no_quote_together_are_refused(mail):
    saved, body, _ = mail
    res = _run("--body-file", body, "--no-quote", "--quote-lang", "de")
    assert res.exit_code == 4, res.output
    assert saved == []


def test_quote_lang_takes_only_de_or_en(mail):
    saved, body, _ = mail
    res = _run("--body-file", body, "--quote-lang", "fr")
    assert res.exit_code == 4, res.output
    assert json.loads(res.output)["error_code"] == "bad_args"
    assert saved == []


def test_preview_needs_no_quote_choice(mail):
    res = _run("--preview")
    assert res.exit_code == 0, res.output


def test_the_libraries_placeholder_for_a_missing_date_counts_as_missing():
    """imap_tools answers a missing or unparseable Date header with 1900-01-01 00:00
    (measured 2026-10-15); `read` turns that into "1900-01-01T00:00:00"."""
    first = quote_block(_orig(date="1900-01-01T00:00:00"), "de").splitlines()[0]
    assert first == "mira@example.org schrieb:"
