"""#50: category / keep-list / sender-list / recipient filters, batching, and the
preview-to-apply binding for delete and move -- without a server."""

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
from mailbox_cleanup.operations.batching import PartialMoveError, move_in_batches
from mailbox_cleanup.operations.delete import PreviewMismatchError, run_delete
from mailbox_cleanup.operations.filters import build_imap_search
from mailbox_cleanup.operations.move import run_move
from mailbox_cleanup.operations.selection import parse_keep, select_messages

NL = {"List-Unsubscribe": ("<https://example.com/u>",)}


def _msg(uid, sender, headers=None, subject="s"):
    return SimpleNamespace(
        uid=str(uid),
        from_=sender,
        subject=subject,
        date=datetime(2026, 1, 1, tzinfo=UTC),
        headers=headers or {},
    )


class FakeMailbox:
    """Returns its messages for any search; records moves; can fail on the nth move."""

    def __init__(self, msgs, fail_on_call=None):
        self._msgs = msgs
        self.moves = []
        self.fetch_limits = []
        self._fail_on_call = fail_on_call
        self.client = SimpleNamespace(capabilities=("MOVE",))  # #73
        self.folder = SimpleNamespace(set=lambda name: None, list=lambda: [])

    def fetch(self, criteria, headers_only, mark_seen, limit, bulk):
        self.fetch_limits.append(limit)
        return self._msgs[:limit] if limit else list(self._msgs)

    def move(self, uids, target):
        if self._fail_on_call is not None and len(self.moves) + 1 == self._fail_on_call:
            raise TimeoutError("server timed out")
        self.moves.append((list(uids), target))


# --- server-side criteria -------------------------------------------------


def test_several_senders_become_one_or_criterion():
    q = str(build_imap_search(sender=("a@x.example", "b@y.example")))
    assert 'OR FROM "a@x.example" FROM "b@y.example"' in q


def test_single_sender_tuple_is_a_plain_from():
    assert str(build_imap_search(sender=("a@x.example",))) == '(FROM "a@x.example")'


def test_recipient_becomes_to():
    assert str(build_imap_search(recipient="alias@relay.example")) == '(TO "alias@relay.example")'


def test_category_alone_counts_as_a_filter():
    assert str(build_imap_search(category="newsletter")) == "(ALL)"


def test_no_filter_at_all_still_raises():
    with pytest.raises(ValueError):
        build_imap_search()


def test_control_character_in_any_sender_is_refused():
    with pytest.raises(ValueError):
        build_imap_search(sender=("ok@x.example", "bad@x.example\r\nA1 DELETE INBOX"))


def test_control_character_in_recipient_is_refused():
    with pytest.raises(ValueError):
        build_imap_search(recipient="a@x.example\n")


# --- keep-list ------------------------------------------------------------


def test_keep_accepts_addresses_and_domains_case_insensitively():
    addrs, domains = parse_keep(["Billing@Shop.example", "@Bank.example"])
    assert addrs == {"billing@shop.example"}
    assert domains == {"bank.example"}


@pytest.mark.parametrize("bad", ["code", "the code", "a@", "@", "a@b@c.example", ""])
def test_keep_refuses_anything_that_is_not_an_address_or_domain(bad):
    with pytest.raises(ValueError):
        parse_keep([bad])


# --- client-side selection ------------------------------------------------


def _mixed():
    return [
        _msg(1, "news@a.example", NL),
        _msg(2, "friend@b.example"),
        _msg(3, "Billing@Shop.example", NL),
        _msg(4, "promo@mail.bank.example", NL),
        _msg(5, "news@a.example", NL),
        _msg(6, "newsletter@c.example"),
    ]


def test_category_selects_only_that_category():
    sel = select_messages(FakeMailbox(_mixed()), folder="INBOX", category="newsletter")
    assert [m.uid for m in sel.messages] == ["1", "3", "4", "5", "6"]


def test_category_newsletter_does_not_reach_a_bare_noreply_sender():
    # A login alert or invoice from a noreply address is not a newsletter.
    msgs = [
        _msg(1, "news@a.example", NL),
        _msg(2, "noreply@bank.example"),
        _msg(3, "no-reply@shop.example", NL),
    ]
    sel = select_messages(FakeMailbox(msgs), folder="INBOX", category="newsletter")
    assert [m.uid for m in sel.messages] == ["1", "3"]


def test_keep_list_excludes_and_is_counted():
    sel = select_messages(
        FakeMailbox(_mixed()),
        folder="INBOX",
        category="newsletter",
        keep=["billing@shop.example", "@bank.example"],
    )
    assert [m.uid for m in sel.messages] == ["1", "5", "6"]
    assert sel.kept_count == 2  # the address, and the subdomain of the kept domain


def test_by_sender_counts_the_selection():
    sel = select_messages(FakeMailbox(_mixed()), folder="INBOX", category="newsletter")
    assert sel.by_sender["news@a.example"] == 2
    assert sel.by_sender["billing@shop.example"] == 1


def test_limit_applies_after_client_side_filters():
    mb = FakeMailbox(_mixed())
    sel = select_messages(
        mb, folder="INBOX", category="newsletter", keep=["news@a.example"], limit=2
    )
    assert [m.uid for m in sel.messages] == ["3", "4"]
    assert mb.fetch_limits == [None]  # the server search is not truncated first


def test_limit_without_client_side_filters_stays_on_the_server():
    mb = FakeMailbox(_mixed())
    select_messages(mb, folder="INBOX", sender=("news@a.example",), limit=2)
    assert mb.fetch_limits == [2]


# --- batching -------------------------------------------------------------


def test_moves_run_in_batches_of_500():
    mb = FakeMailbox([])
    moved = move_in_batches(mb, [str(i) for i in range(1200)], "Trash")
    assert [len(u) for u, _ in mb.moves] == [500, 500, 200]
    assert len(moved) == 1200


def test_failure_in_batch_two_reports_what_moved():
    mb = FakeMailbox([], fail_on_call=2)
    with pytest.raises(PartialMoveError) as exc:
        move_in_batches(mb, [str(i) for i in range(1200)], "Trash")
    assert exc.value.moved == [str(i) for i in range(500)]
    assert len(exc.value.remaining) == 700


# --- preview-to-apply binding --------------------------------------------


class TrashMailbox(FakeMailbox):
    def __init__(self, msgs, **kw):
        super().__init__(msgs, **kw)
        self.folder = SimpleNamespace(
            set=lambda name: None,
            list=lambda: [SimpleNamespace(name="Trash", flags=("\\Trash",))],
        )


def test_apply_refuses_when_the_set_changed_since_the_preview():
    mb = TrashMailbox(_mixed())
    with pytest.raises(PreviewMismatchError):
        run_delete(mb, folder="INBOX", category="newsletter", apply=True, expect_count=4)
    assert mb.moves == []


def test_apply_moves_when_the_count_matches():
    mb = TrashMailbox(_mixed())
    res = run_delete(mb, folder="INBOX", category="newsletter", apply=True, expect_count=5)
    assert sorted(res.affected_uids) == ["1", "3", "4", "5", "6"]
    assert mb.moves and mb.moves[0][1] == "Trash"


def test_move_has_the_same_filters_and_binding():
    mb = FakeMailbox(_mixed())
    with pytest.raises(PreviewMismatchError):
        run_move(
            mb, folder="INBOX", target="Archiv", category="newsletter", apply=True, expect_count=1
        )
    res = run_move(
        mb, folder="INBOX", target="Archiv", category="newsletter", keep=["@a.example"], apply=True
    )
    assert res.affected_uids == ["3", "4", "6"]


# --- CLI ------------------------------------------------------------------


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
    state = {}

    def use(mb):
        @contextmanager
        def _connect(creds, *, port=993):
            yield mb

        monkeypatch.setattr(ccli, "imap_connect", _connect)
        state["mb"] = mb

    return SimpleNamespace(log=log, use=use)


def _records(log):
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]


def test_cli_dry_run_reports_kept_count_and_senders(cli_env):
    cli_env.use(TrashMailbox(_mixed()))
    res = CliRunner().invoke(
        cli,
        ["delete", "--category", "newsletter", "--keep", "@bank.example", "--json"],
    )
    assert res.exit_code == 0, res.output
    out = json.loads(res.output)
    assert out["dry_run"] is True
    assert out["affected_count"] == 4
    assert out["kept_count"] == 1
    assert out["by_sender"]["news@a.example"] == 2
    assert not cli_env.log.exists()


def test_cli_keep_bare_word_is_bad_args(cli_env):
    cli_env.use(TrashMailbox(_mixed()))
    res = CliRunner().invoke(
        cli, ["delete", "--category", "newsletter", "--keep", "code", "--json"]
    )
    assert res.exit_code == 4, res.output
    assert json.loads(res.output)["error_code"] == "bad_args"


def test_cli_expect_count_mismatch_moves_nothing(cli_env):
    mb = TrashMailbox(_mixed())
    cli_env.use(mb)
    res = CliRunner().invoke(
        cli, ["delete", "--category", "newsletter", "--apply", "--expect-count", "3", "--json"]
    )
    assert res.exit_code == 4, res.output
    assert json.loads(res.output)["error_code"] == "preview_mismatch"
    assert mb.moves == []


def test_cli_partial_failure_is_exit_5_and_logs_what_moved(cli_env):
    msgs = [_msg(i, f"n{i}@a.example", NL) for i in range(700)]
    cli_env.use(TrashMailbox(msgs, fail_on_call=2))
    res = CliRunner().invoke(cli, ["delete", "--category", "newsletter", "--apply", "--json"])
    assert res.exit_code == 5, res.output
    out = json.loads(res.output)
    assert out["error_code"] == "partial_failure"
    assert out["moved_count"] == 500
    (rec,) = _records(cli_env.log)
    assert rec["result"] == "partial_failure"
    assert len(rec["affected_uids"]) == 500
    assert rec["args"]["category"] == "newsletter"


def test_cli_several_senders_and_recipient_are_logged(cli_env):
    cli_env.use(TrashMailbox(_mixed()))
    res = CliRunner().invoke(
        cli,
        [
            "delete",
            "--sender", "news@a.example",
            "--sender", "newsletter@c.example",
            "--recipient", "alias@relay.example",
            "--apply",
            "--json",
        ],
    )  # fmt: skip
    assert res.exit_code == 0, res.output
    (rec,) = _records(cli_env.log)
    assert rec["args"]["sender"] == ["news@a.example", "newsletter@c.example"]
    assert rec["args"]["recipient"] == "alias@relay.example"
    assert rec["result"] == "success"


def test_cli_move_accepts_the_new_filters(cli_env):
    mb = FakeMailbox(_mixed())
    cli_env.use(mb)
    res = CliRunner().invoke(
        cli, ["move", "--to", "Archiv", "--category", "newsletter", "--apply", "--json"]
    )
    assert res.exit_code == 0, res.output
    assert mb.moves[0][1] == "Archiv"


def test_cli_server_without_move_or_uidplus_is_exit_5_and_moves_nothing(cli_env):
    """#73: a move that cannot be scoped to our UIDs is refused, and nothing is touched."""
    mb = TrashMailbox(_mixed())
    mb.client = SimpleNamespace(capabilities=("IMAP4REV1",))
    cli_env.use(mb)
    res = CliRunner().invoke(cli, ["delete", "--category", "newsletter", "--apply", "--json"])
    assert res.exit_code == 5, res.output
    assert json.loads(res.output)["error_code"] == "move_unsupported"
    assert mb.moves == []
