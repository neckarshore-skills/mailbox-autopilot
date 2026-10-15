"""`manage draft` answers everyone by default (Founder decision 2026-10-15, design
docs/2026-10-15-reply-all-design.md).

The recipients of a reply come from the answered mail, which its sender controls. So the
program computes them; the caller can only take addresses away (`--drop`) or narrow to the
sender (`--sender-only`), never add one.
"""

import json
from contextlib import contextmanager

import pytest
from click.testing import CliRunner

from mailbox_cleanup.auth import Credentials
from mailbox_cleanup.cli import cli
from mailbox_cleanup.config import Account
from mailbox_cleanup.manage import cli as mcli
from mailbox_cleanup.manage.draft import (
    MAX_RECIPIENTS,
    RecipientError,
    build_reply,
    plan_recipients,
)
from mailbox_cleanup.manage.envelope import wrap
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
        date="",
        text="Hallo",
    )
    base.update(kw)
    return Message(**base)


# --- the plan: who gets the reply ------------------------------------------------------


def test_message_has_cc_defaulting_to_empty():
    assert _orig().cc == ()


def test_reply_all_keeps_other_to_recipients_and_the_cc():
    o = _orig(to=(ME, "ben@example.org"), cc=("cara@example.net",))
    plan = plan_recipients(o, own_addr=ME)
    assert plan.to == ["mira@example.org", "ben@example.org"]
    assert plan.cc == ["cara@example.net"]
    assert plan.warnings == []


def test_own_address_is_removed_regardless_of_case():
    o = _orig(to=("Me@Example.COM", "ben@example.org"), cc=("ME@example.com",))
    plan = plan_recipients(o, own_addr=ME)
    assert plan.to == ["mira@example.org", "ben@example.org"]
    assert plan.cc == []


def test_duplicates_across_from_to_and_cc_appear_once():
    o = _orig(to=(ME, "ben@example.org", "BEN@example.org"), cc=("mira@example.org",))
    plan = plan_recipients(o, own_addr=ME)
    assert plan.to == ["mira@example.org", "ben@example.org"]
    assert plan.cc == []


def test_reply_to_replaces_the_sender_as_main_recipient():
    o = _orig(reply_to="jobs@example.org", to=(ME, "ben@example.org"))
    plan = plan_recipients(o, own_addr=ME)
    assert plan.to == ["jobs@example.org", "ben@example.org"]
    assert "mira@example.org" not in plan.to + plan.cc


def test_mail_the_owner_sent_goes_to_its_recipients_not_back_to_the_owner():
    o = _orig(sender=ME, to=("ben@example.org",), cc=("cara@example.net",))
    plan = plan_recipients(o, own_addr=ME)
    assert plan.to == ["ben@example.org"]
    assert plan.cc == ["cara@example.net"]


def test_sender_only_drops_everyone_else():
    o = _orig(to=(ME, "ben@example.org"), cc=("cara@example.net",))
    plan = plan_recipients(o, own_addr=ME, sender_only=True)
    assert plan.to == ["mira@example.org"]
    assert plan.cc == []


def test_sender_only_on_the_owners_own_mail_answers_its_recipients():
    o = _orig(sender=ME, to=("ben@example.org",), cc=("cara@example.net",))
    plan = plan_recipients(o, own_addr=ME, sender_only=True)
    assert plan.to == ["ben@example.org"]
    assert plan.cc == []


HOSTILE = [
    "=?utf-8?q?evil=40x.org=2C_b?=@example.org",
    "a(b@example.org",
    "a@b, c@d",
    "",
]


@pytest.mark.parametrize("bad", HOSTILE)
def test_unusable_cc_address_is_left_out_with_a_count_never_the_value(bad):
    o = _orig(to=(ME, "ben@example.org"), cc=(bad, "cara@example.net"))
    plan = plan_recipients(o, own_addr=ME)
    assert plan.to == ["mira@example.org", "ben@example.org"]
    assert plan.cc == ["cara@example.net"]
    if bad:
        assert plan.warnings == ["1 recipient address of the original is unusable and left out"]
        assert all(bad not in w for w in plan.warnings)
    else:
        assert plan.warnings == []


def test_unusable_sender_with_other_recipients_still_answers_them():
    o = _orig(sender='"x" <', to=(ME, "ben@example.org"))
    plan = plan_recipients(o, own_addr=ME)
    assert plan.to == ["ben@example.org"]
    assert plan.warnings == ["original has no usable sender address"]


def test_unusable_sender_and_nobody_else_keeps_the_old_warning():
    plan = plan_recipients(_orig(sender='"x" <'), own_addr=ME)
    assert plan.to == [] and plan.cc == []
    assert plan.warnings == ["original has no usable sender address; draft has no recipient"]


def test_more_than_the_cap_is_refused_not_truncated():
    many = tuple(f"p{i}@example.org" for i in range(MAX_RECIPIENTS))  # + sender = cap + 1
    with pytest.raises(RecipientError) as e:
        plan_recipients(_orig(to=(ME, *many)), own_addr=ME)
    assert e.value.code == "too_many_recipients"
    assert "--sender-only" in str(e.value)


def test_exactly_the_cap_is_allowed():
    many = tuple(f"p{i}@example.org" for i in range(MAX_RECIPIENTS - 1))
    plan = plan_recipients(_orig(to=(ME, *many)), own_addr=ME)
    assert len(plan.to) + len(plan.cc) == MAX_RECIPIENTS


def test_drop_takes_an_address_away_case_insensitively():
    o = _orig(to=(ME, "ben@example.org"), cc=("cara@example.net",))
    plan = plan_recipients(o, own_addr=ME, drop=("CARA@example.net",))
    assert plan.to == ["mira@example.org", "ben@example.org"]
    assert plan.cc == []


def test_drop_of_an_address_that_is_not_a_recipient_is_refused():
    with pytest.raises(RecipientError) as e:
        plan_recipients(_orig(), own_addr=ME, drop=("evil@example.net",))
    assert e.value.code == "not_a_recipient"
    assert "evil@example.net" not in str(e.value)


def test_drop_that_is_not_one_bare_address_is_refused():
    with pytest.raises(RecipientError) as e:
        plan_recipients(_orig(), own_addr=ME, drop=("a@b, c@d",))
    assert e.value.code == "not_a_recipient"


def test_drop_below_the_cap_makes_a_large_mail_answerable():
    many = tuple(f"p{i}@example.org" for i in range(MAX_RECIPIENTS))
    plan = plan_recipients(_orig(to=(ME, *many)), own_addr=ME, drop=("p0@example.org",))
    assert len(plan.to) + len(plan.cc) == MAX_RECIPIENTS


def test_dropping_every_to_recipient_moves_the_first_cc_up():
    o = _orig(cc=("cara@example.net", "dan@example.net"))
    plan = plan_recipients(o, own_addr=ME, drop=("mira@example.org",))
    assert plan.to == ["cara@example.net"]
    assert plan.cc == ["dan@example.net"]


# --- the built draft -----------------------------------------------------------------


def test_built_draft_carries_to_and_cc_as_bare_addresses_and_no_bcc():
    o = _orig(to=(ME, "Ben <ben@example.org>"), cc=("cara@example.net",))
    plan = plan_recipients(o, own_addr=ME)
    msg, _ = build_reply(o, from_addr=ME, body="x", recipients=plan)
    assert [(a.addr_spec, a.display_name) for a in msg["To"].addresses] == [
        ("mira@example.org", ""),
        ("ben@example.org", ""),
    ]
    assert [a.addr_spec for a in msg["Cc"].addresses] == ["cara@example.net"]
    assert msg["Bcc"] is None
    assert b"\nBcc:" not in msg.as_bytes()


def test_build_reply_without_a_plan_answers_everyone():
    o = _orig(to=(ME, "ben@example.org"))
    msg, _ = build_reply(o, from_addr=ME, body="x")
    assert [a.addr_spec for a in msg["To"].addresses] == ["mira@example.org", "ben@example.org"]


def test_build_reply_with_no_recipient_sets_no_to_header():
    msg, warnings = build_reply(_orig(sender=""), from_addr=ME, body="x")
    assert msg["To"] is None and msg["Cc"] is None
    assert warnings == ["original has no usable sender address; draft has no recipient"]


# --- the command -----------------------------------------------------------------------


@pytest.fixture
def audit(tmp_path, monkeypatch):
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
    return log


@contextmanager
def _fake_connect(creds, *, port=993):
    yield object()


def _records(log):
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]


@pytest.fixture
def mail(monkeypatch):
    """A mail with one other To and one Cc; captures what would be appended."""
    saved = []
    o = _orig(to=(ME, "ben@example.org"), cc=("cara@example.net",))
    monkeypatch.setattr(mcli, "imap_connect", _fake_connect)
    monkeypatch.setattr(mcli, "read_message", lambda mb, *, uid, folder: o)

    def _save(mb, msg):
        saved.append(msg)
        return "Drafts"

    monkeypatch.setattr(mcli, "save_draft", _save)
    return saved


def _body(tmp_path):
    p = tmp_path / "body.txt"
    p.write_text("Antwort", encoding="utf-8")
    return str(p)


def test_preview_writes_nothing_and_needs_no_text(audit, mail):
    res = CliRunner().invoke(cli, ["manage", "draft", "--uid", "1", "--preview", "--json"])
    assert res.exit_code == 0, res.output
    out = json.loads(res.output)
    assert out["preview"] is True
    assert out["to"] == [wrap("mira@example.org"), wrap("ben@example.org")]
    assert out["cc"] == [wrap("cara@example.net")]
    assert "drafts_folder" not in out
    assert mail == []
    (rec,) = _records(audit)
    assert rec["subcommand"] == "manage.draft" and rec["result"] == "preview"


def test_write_reports_the_recipients_read_back_from_the_draft(audit, mail, tmp_path):
    res = CliRunner().invoke(
        cli, ["manage", "draft", "--uid", "1", "--body-file", _body(tmp_path), "--json"]
    )
    assert res.exit_code == 0, res.output
    out = json.loads(res.output)
    assert out["to"] == [wrap("mira@example.org"), wrap("ben@example.org")]
    assert out["cc"] == [wrap("cara@example.net")]
    (msg,) = mail
    assert [a.addr_spec for a in msg["Cc"].addresses] == ["cara@example.net"]


def test_write_without_text_is_refused_before_any_imap_call(audit, monkeypatch):
    def _no_connect(*a, **kw):
        raise AssertionError("mailbox contacted")

    monkeypatch.setattr(mcli, "imap_connect", _no_connect)
    res = CliRunner().invoke(cli, ["manage", "draft", "--uid", "1", "--json"])
    assert res.exit_code == 4, res.output
    assert json.loads(res.output)["error_code"] == "bad_args"


def test_sender_only_and_drop_reach_the_draft(audit, mail, tmp_path):
    res = CliRunner().invoke(
        cli,
        [
            "manage", "draft", "--uid", "1", "--drop", "cara@example.net",
            "--body-file", _body(tmp_path), "--json",
        ],
    )  # fmt: skip
    assert res.exit_code == 0, res.output
    assert json.loads(res.output)["cc"] == []
    res = CliRunner().invoke(
        cli,
        [
            "manage",
            "draft",
            "--uid",
            "1",
            "--sender-only",
            "--body-file",
            _body(tmp_path),
            "--json",
        ],  # noqa: E501
    )
    assert res.exit_code == 0, res.output
    assert json.loads(res.output)["to"] == [wrap("mira@example.org")]
    recs = _records(audit)
    assert recs[0]["arg_keys"] == ["body_file", "drop", "uid"]
    assert recs[1]["arg_keys"] == ["body_file", "sender_only", "uid"]
    assert "cara@example.net" not in audit.read_text(encoding="utf-8")


def test_drop_of_a_foreign_address_is_refused_and_writes_nothing(audit, mail, tmp_path):
    res = CliRunner().invoke(
        cli,
        [
            "manage", "draft", "--uid", "1", "--drop", "evil@example.net",
            "--body-file", _body(tmp_path), "--json",
        ],
    )  # fmt: skip
    assert res.exit_code == 4, res.output
    out = json.loads(res.output)
    assert out["error_code"] == "not_a_recipient"
    assert "evil@example.net" not in res.output
    assert mail == []
    (rec,) = _records(audit)
    assert rec["result"] == "error" and rec["error"] == "not_a_recipient"


def test_too_many_recipients_is_refused_and_writes_nothing(audit, monkeypatch, tmp_path):
    saved = []
    many = tuple(f"p{i}@example.org" for i in range(MAX_RECIPIENTS))
    monkeypatch.setattr(mcli, "imap_connect", _fake_connect)
    monkeypatch.setattr(mcli, "read_message", lambda mb, *, uid, folder: _orig(to=(ME, *many)))
    monkeypatch.setattr(mcli, "save_draft", lambda mb, msg: saved.append(msg) or "Drafts")
    res = CliRunner().invoke(
        cli, ["manage", "draft", "--uid", "1", "--body-file", _body(tmp_path), "--json"]
    )
    assert res.exit_code == 4, res.output
    assert json.loads(res.output)["error_code"] == "too_many_recipients"
    assert saved == []


def test_drop_that_is_not_an_address_is_a_control_char_safe_refusal(audit, mail, tmp_path):
    res = CliRunner().invoke(
        cli,
        [
            "manage", "draft", "--uid", "1", "--drop", "a@b\r\nBcc: x@y",
            "--body-file", _body(tmp_path), "--json",
        ],
    )  # fmt: skip
    assert res.exit_code == 4, res.output
    assert mail == []


# --- review round 1: an emptied list, and a preview that shows an over-long list -------


def test_dropping_every_recipient_is_refused_not_an_empty_draft():
    with pytest.raises(RecipientError) as e:
        plan_recipients(_orig(), own_addr=ME, drop=("mira@example.org",))
    assert e.value.code == "no_recipient_left"


def test_sender_only_then_dropping_the_sender_is_refused():
    o = _orig(to=(ME, "ben@example.org"))
    with pytest.raises(RecipientError) as e:
        plan_recipients(o, own_addr=ME, sender_only=True, drop=("mira@example.org",))
    assert e.value.code == "no_recipient_left"


def test_plan_without_the_cap_lists_everyone_and_marks_it():
    many = tuple(f"p{i}@example.org" for i in range(MAX_RECIPIENTS))
    plan = plan_recipients(_orig(to=(ME, *many)), own_addr=ME, enforce_cap=False)
    assert len(plan.to) == MAX_RECIPIENTS + 1
    assert plan.over_limit is True


def test_preview_of_an_over_long_list_shows_it_so_drop_has_something_to_pick(audit, monkeypatch):
    many = tuple(f"p{i}@example.org" for i in range(MAX_RECIPIENTS))
    monkeypatch.setattr(mcli, "imap_connect", _fake_connect)
    monkeypatch.setattr(mcli, "read_message", lambda mb, *, uid, folder: _orig(to=(ME, *many)))
    res = CliRunner().invoke(cli, ["manage", "draft", "--uid", "1", "--preview", "--json"])
    assert res.exit_code == 0, res.output
    out = json.loads(res.output)
    assert len(out["to"]) == MAX_RECIPIENTS + 1
    assert out["over_limit"] is True
    res = CliRunner().invoke(
        cli, ["manage", "draft", "--uid", "1", "--preview", "--drop", "p0@example.org", "--json"]
    )
    assert json.loads(res.output)["over_limit"] is False


def test_cli_refuses_an_emptied_list_and_writes_nothing(audit, mail, tmp_path):
    res = CliRunner().invoke(
        cli,
        [
            "manage", "draft", "--uid", "1", "--sender-only", "--drop", "mira@example.org",
            "--body-file", _body(tmp_path), "--json",
        ],
    )  # fmt: skip
    assert res.exit_code == 4, res.output
    assert json.loads(res.output)["error_code"] == "no_recipient_left"
    assert mail == []
