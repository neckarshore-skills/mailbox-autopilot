"""#55: archive, bounces and dedupe bind --apply to the confirmed preview (--expect-count),
the same way delete and move do since #54 -- without a server."""

import json
from contextlib import contextmanager
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from mailbox_cleanup import cli as ccli
from mailbox_cleanup.auth import Credentials
from mailbox_cleanup.cli import cli
from mailbox_cleanup.config import Account
from mailbox_cleanup.operations.archive import run_archive
from mailbox_cleanup.operations.bounces import run_bounces
from mailbox_cleanup.operations.dedupe import run_dedupe
from mailbox_cleanup.operations.delete import PreviewMismatchError
from tests._imap_fakes import move_capable_client

NOW = datetime(2026, 5, 4, tzinfo=UTC)


def _msg(uid, *, sender="x@y.example", subject="s", date=None, mid=None):
    return SimpleNamespace(
        uid=str(uid),
        from_=sender,
        subject=subject,
        date=date or datetime(2023, 6, 1, tzinfo=UTC),
        headers={"message-id": (mid,)} if mid else {},
    )


class Mailbox:
    """Any search returns all messages; records folder creations and moves."""

    def __init__(self, msgs):
        self._msgs = msgs
        self.moves = []
        self.created = []
        self.client = move_capable_client(self)
        folders = [
            SimpleNamespace(name="INBOX", flags=()),
            SimpleNamespace(name="Archive", flags=("\\Archive",)),
            SimpleNamespace(name="Trash", flags=("\\Trash",)),
        ]
        self.folder = SimpleNamespace(
            set=lambda name: None,
            list=lambda: folders,
            exists=lambda name: False,
            create=self.created.append,
        )

    def fetch(self, *args, **kwargs):
        return list(self._msgs)

    def move(self, uids, target):
        self.moves.append((list(uids), target))


def _archive_mail():
    return [
        _msg(1, date=datetime(2023, 6, 1, tzinfo=UTC)),
        _msg(2, date=datetime(2023, 7, 1, tzinfo=UTC)),
        _msg(3, date=datetime(2024, 6, 1, tzinfo=UTC)),
    ]


def _bounce_mail():
    return [
        _msg(1, sender="mailer-daemon@mx.example"),
        _msg(2, sender="mailer-daemon@mx.example"),
        _msg(3, sender="anna@example.com"),
    ]


def _dupe_mail():
    return [
        _msg(1, mid="<a@x.example>", date=datetime(2025, 1, 1, tzinfo=UTC)),
        _msg(2, mid="<a@x.example>", date=datetime(2025, 1, 2, tzinfo=UTC)),
        _msg(3, mid="<a@x.example>", date=datetime(2025, 1, 3, tzinfo=UTC)),
        _msg(4, mid="<b@x.example>", date=datetime(2025, 1, 4, tzinfo=UTC)),
    ]


RUNS = {
    "archive": (
        _archive_mail,
        lambda mb, **kw: run_archive(mb, folder="INBOX", older_than="12m", now=NOW, **kw),
        3,
    ),
    "bounces": (_bounce_mail, lambda mb, **kw: run_bounces(mb, folder="INBOX", **kw), 2),
    "dedupe": (_dupe_mail, lambda mb, **kw: run_dedupe(mb, folder="INBOX", **kw), 2),
}


# --- operations -------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(RUNS))
def test_a_changed_count_moves_and_creates_nothing(name):
    mails, run, count = RUNS[name]
    mb = Mailbox(mails())
    with pytest.raises(PreviewMismatchError) as exc:
        run(mb, apply=True, expect_count=count + 1)
    assert (exc.value.expected, exc.value.found) == (count + 1, count)
    assert mb.moves == []
    assert mb.created == []  # archive: no empty Archive/<year> left behind either


@pytest.mark.parametrize("name", sorted(RUNS))
def test_the_confirmed_count_applies(name):
    mails, run, count = RUNS[name]
    mb = Mailbox(mails())
    run(mb, apply=True, expect_count=count)
    assert sum(len(uids) for uids, _ in mb.moves) == count


@pytest.mark.parametrize("name", sorted(RUNS))
def test_without_the_flag_apply_is_unchanged(name):
    mails, run, count = RUNS[name]
    mb = Mailbox(mails())
    run(mb, apply=True)
    assert sum(len(uids) for uids, _ in mb.moves) == count


@pytest.mark.parametrize("name", sorted(RUNS))
def test_a_dry_run_ignores_the_flag(name):
    mails, run, _ = RUNS[name]
    mb = Mailbox(mails())
    run(mb, apply=False, expect_count=999)
    assert mb.moves == []


# --- CLI ----------------------------------------------------------------------


@pytest.fixture
def cli_env(tmp_path, monkeypatch):
    log = tmp_path / "audit.log"
    monkeypatch.setenv("MAILBOX_CLEANUP_AUDIT_LOG", str(log))
    monkeypatch.setattr(
        ccli,
        "resolve_account_and_credentials",
        lambda **kw: (
            Account(alias="t", email="test@localhost", server="imap.example.com", port=993),
            Credentials(email="test@localhost", password="x", server="imap.example.com"),
        ),
    )

    def use(mb):
        @contextmanager
        def _connect(creds, *, port=993):
            yield mb

        monkeypatch.setattr(ccli, "imap_connect", _connect)

    return SimpleNamespace(log=log, use=use)


ARGV = {
    "archive": ["archive", "--older-than", "12m"],
    "bounces": ["bounces"],
    "dedupe": ["dedupe"],
}


@pytest.mark.parametrize("name", sorted(ARGV))
def test_cli_a_changed_count_is_preview_mismatch_exit_4(cli_env, name):
    mails, _, count = RUNS[name]
    mb = Mailbox(mails())
    cli_env.use(mb)
    argv = [*ARGV[name], "--apply", "--expect-count", str(count + 1), "--json"]
    res = CliRunner().invoke(cli, argv)
    assert res.exit_code == 4, res.output
    out = json.loads(res.output)
    assert out["error_code"] == "preview_mismatch"
    assert (out["expected"], out["found"]) == (count + 1, count)
    assert mb.moves == []
    assert not cli_env.log.exists()  # nothing moved, so nothing to record as moved


@pytest.mark.parametrize("name", sorted(ARGV))
def test_cli_the_dry_run_affected_count_is_what_apply_accepts(cli_env, name):
    """The skill passes the dry-run's affected_count; it must be the number apply checks."""
    mails, _, count = RUNS[name]
    if name == "archive":
        # archive's dry-run must run at the same `now` as the apply; the CLI uses the
        # real clock, so give every message a date far enough in the past.
        mails = lambda: [  # noqa: E731
            _msg(1, date=datetime(2020, 6, 1, tzinfo=UTC)),
            _msg(2, date=datetime(2020, 7, 1, tzinfo=UTC)),
            _msg(3, date=datetime(2021, 6, 1, tzinfo=UTC)),
        ]
    cli_env.use(Mailbox(mails()))
    dry = CliRunner().invoke(cli, [*ARGV[name], "--json"])
    assert dry.exit_code == 0, dry.output
    affected = json.loads(dry.output)["affected_count"]
    assert affected == count

    mb = Mailbox(mails())
    cli_env.use(mb)
    argv = [*ARGV[name], "--apply", "--expect-count", str(affected), "--json"]
    res = CliRunner().invoke(cli, argv)
    assert res.exit_code == 0, res.output
    assert sum(len(uids) for uids, _ in mb.moves) == count
