"""Persistent keep list per account in the configuration (Founder decision 2026-10-13,
scope and upkeep decided 2026-10-15: per account, maintained by `config keep`).

A sender on an account's keep list is never moved by delete, move, unsubscribe or
bounces on that account. The list survives every other config command.
"""

import json
from contextlib import contextmanager
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from mailbox_cleanup import cli as ccli
from mailbox_cleanup.auth import Credentials
from mailbox_cleanup.cli import cli
from mailbox_cleanup.config import (
    DEFAULT_CONFIG_PATH_ENV,
    Account,
    Config,
    ConfigError,
    load_config,
    save_config,
    validate_config,
)
from mailbox_cleanup.operations.bounces import run_bounces
from mailbox_cleanup.operations.unsubscribe import collect_unsub_targets
from tests._imap_fakes import move_capable_client

NL = {"List-Unsubscribe": ("<https://example.com/u>",)}


def _acc(alias="a", email="a@example.com", keep=()):
    return Account(alias=alias, email=email, server="imap.example.com", keep=tuple(keep))


# --- config schema ----------------------------------------------------------


def _raw(keep):
    return {
        "schema_version": 1,
        "default": "a",
        "accounts": [
            {"alias": "a", "email": "a@example.com", "server": "imap.example.com", "keep": keep}
        ],
    }


def test_account_without_keep_has_an_empty_list():
    raw = _raw([])
    del raw["accounts"][0]["keep"]
    assert validate_config(raw).accounts[0].keep == ()


def test_keep_entries_are_loaded_lowercased():
    cfg = validate_config(_raw(["Bank@Example.com", "@Shop.Example"]))
    assert cfg.accounts[0].keep == ("bank@example.com", "@shop.example")


def test_bare_word_in_config_keep_fails_on_load():
    with pytest.raises(ConfigError, match="keep"):
        validate_config(_raw(["The Code"]))


def test_keep_must_be_a_list():
    with pytest.raises(ConfigError, match="keep"):
        validate_config(_raw("bank@example.com"))


def test_keep_survives_save_and_load(tmp_path, monkeypatch):
    monkeypatch.setenv(DEFAULT_CONFIG_PATH_ENV, str(tmp_path / "cfg.json"))
    save_config(Config(default="a", accounts=(_acc(keep=["bank@example.com"]),)))
    assert load_config().accounts[0].keep == ("bank@example.com",)


# --- config keep commands ---------------------------------------------------


@pytest.fixture
def two_accounts(tmp_path, monkeypatch):
    monkeypatch.setenv(DEFAULT_CONFIG_PATH_ENV, str(tmp_path / "cfg.json"))
    monkeypatch.delenv("MAILBOX_CLEANUP_ACCOUNT", raising=False)
    save_config(
        Config(
            default="a",
            accounts=(_acc(), _acc(alias="b", email="b@example.com")),
        )
    )


def _keep(alias):
    return next(a for a in load_config().accounts if a.alias == alias).keep


def test_keep_add_writes_to_the_named_account_only(two_accounts):
    r = CliRunner().invoke(
        cli, ["config", "keep", "add", "--account", "b", "Bank@Example.com", "@shop.example"]
    )
    assert r.exit_code == 0, r.output
    assert _keep("b") == ("bank@example.com", "@shop.example")
    assert _keep("a") == ()


def test_keep_add_without_account_uses_the_default(two_accounts):
    r = CliRunner().invoke(cli, ["config", "keep", "add", "bank@example.com"])
    assert r.exit_code == 0, r.output
    assert _keep("a") == ("bank@example.com",)


def test_keep_add_twice_stores_once(two_accounts):
    CliRunner().invoke(cli, ["config", "keep", "add", "bank@example.com"])
    r = CliRunner().invoke(cli, ["config", "keep", "add", "BANK@example.com"])
    assert r.exit_code == 0, r.output
    assert _keep("a") == ("bank@example.com",)


def test_keep_add_bare_word_is_refused_and_writes_nothing(two_accounts):
    r = CliRunner().invoke(cli, ["config", "keep", "add", "bank@example.com", "The Code", "--json"])
    assert r.exit_code == 4, r.output
    assert json.loads(r.output)["error_code"] == "bad_args"
    assert _keep("a") == ()


def test_keep_add_unknown_account_is_refused(two_accounts):
    r = CliRunner().invoke(
        cli, ["config", "keep", "add", "--account", "zz", "bank@example.com", "--json"]
    )
    assert r.exit_code == 4, r.output
    assert json.loads(r.output)["error_code"] == "unknown_account"


def test_keep_remove(two_accounts):
    CliRunner().invoke(cli, ["config", "keep", "add", "bank@example.com", "@shop.example"])
    r = CliRunner().invoke(cli, ["config", "keep", "remove", "Bank@example.com"])
    assert r.exit_code == 0, r.output
    assert _keep("a") == ("@shop.example",)


def test_keep_remove_of_an_absent_entry_is_refused(two_accounts):
    r = CliRunner().invoke(cli, ["config", "keep", "remove", "nobody@example.com", "--json"])
    assert r.exit_code == 4, r.output
    assert json.loads(r.output)["error_code"] == "not_in_keep_list"


def test_keep_list_json(two_accounts):
    CliRunner().invoke(cli, ["config", "keep", "add", "--account", "b", "bank@example.com"])
    r = CliRunner().invoke(cli, ["config", "keep", "list", "--account", "b", "--json"])
    assert r.exit_code == 0, r.output
    assert json.loads(r.output) == {"account": "b", "keep": ["bank@example.com"]}


def test_set_default_keeps_the_keep_list(two_accounts):
    CliRunner().invoke(cli, ["config", "keep", "add", "bank@example.com"])
    r = CliRunner().invoke(cli, ["config", "set-default", "b"])
    assert r.exit_code == 0, r.output
    assert _keep("a") == ("bank@example.com",)


def test_rename_keeps_the_keep_list(two_accounts):
    CliRunner().invoke(cli, ["config", "keep", "add", "bank@example.com"])
    r = CliRunner().invoke(cli, ["config", "rename", "a", "c"])
    assert r.exit_code == 0, r.output
    assert _keep("c") == ("bank@example.com",)


# --- delete and move honour the account's list -------------------------------


def _msg(uid, sender, headers=None, subject="s"):
    return SimpleNamespace(
        uid=str(uid),
        from_=sender,
        subject=subject,
        date=datetime(2026, 1, 1, tzinfo=UTC),
        headers=headers or {},
    )


class _MB:
    def __init__(self, msgs):
        self._msgs = msgs
        self.moves = []
        self.client = move_capable_client(self)  # #73/#79
        self.folder = SimpleNamespace(
            set=lambda name: None,
            list=lambda: [SimpleNamespace(name="Trash", flags=("\\Trash",), delim="/")],
        )

    def fetch(self, *a, **kw):
        return list(self._msgs)

    def move(self, uids, target):
        self.moves.append((list(uids), target))


@pytest.fixture
def keep_env(tmp_path, monkeypatch):
    log = tmp_path / "audit.log"
    monkeypatch.setenv("MAILBOX_CLEANUP_AUDIT_LOG", str(log))
    state = SimpleNamespace(log=log, mb=None)

    def use(mb, keep):
        monkeypatch.setattr(
            ccli,
            "resolve_account_and_credentials",
            lambda **kw: (
                _acc(alias="t", email="t@example.com", keep=keep),
                Credentials(email="t@example.com", password="x", server="imap.example.com"),
            ),
        )

        @contextmanager
        def _connect(creds, *, port=993):
            yield mb

        monkeypatch.setattr(ccli, "imap_connect", _connect)
        state.mb = mb

    state.use = use
    return state


def _news():
    return [
        _msg(1, "news@a.example", NL),
        _msg(2, "promo@mail.bank.example", NL),
        _msg(3, "letters@shop.example", NL),
    ]


def test_delete_keeps_senders_on_the_account_list(keep_env):
    keep_env.use(_MB(_news()), keep=["@bank.example"])
    r = CliRunner().invoke(cli, ["delete", "--category", "newsletter", "--json"])
    assert r.exit_code == 0, r.output
    out = json.loads(r.output)
    assert out["affected_uids"] == ["1", "3"]
    assert out["kept_count"] == 1
    assert out["kept_by_config"] == 1
    assert out["keep_config"] == ["@bank.example"]


def test_delete_flag_and_config_lists_add_up(keep_env):
    mb = _MB(_news())
    keep_env.use(mb, keep=["@bank.example"])
    r = CliRunner().invoke(
        cli,
        [
            "delete", "--category", "newsletter",
            "--keep", "letters@shop.example", "--apply", "--json",
        ],
    )  # fmt: skip
    assert r.exit_code == 0, r.output
    out = json.loads(r.output)
    assert out["affected_uids"] == ["1"]
    assert out["kept_count"] == 2
    assert out["kept_by_config"] == 1
    assert mb.moves == [(["1"], "Trash")]
    (rec,) = [json.loads(x) for x in keep_env.log.read_text().splitlines()]
    # #75: names and the list's length, never the addresses
    assert "keep" in rec["arg_keys"]
    assert rec["keep_config_count"] == 1
    assert "example" not in json.dumps(rec)


def test_move_keeps_senders_on_the_account_list(keep_env):
    mb = _MB(_news())
    keep_env.use(mb, keep=["news@a.example"])
    r = CliRunner().invoke(
        cli, ["move", "--to", "Archiv", "--category", "newsletter", "--apply", "--json"]
    )
    assert r.exit_code == 0, r.output
    assert mb.moves == [(["2", "3"], "Archiv")]


# --- unsubscribe and bounces -------------------------------------------------


class _UnsubMB:
    def __init__(self, msgs):
        self._msgs = msgs
        self.folder = SimpleNamespace(set=lambda name: None)

    def fetch(self, *a, **kw):
        return list(self._msgs)


def test_collect_unsub_targets_skips_kept_senders():
    msgs = [
        _msg(1, "news@bank.example", {"List-Unsubscribe": ("<https://bank.example/u>",)}),
        _msg(2, "news@other.example", {"List-Unsubscribe": ("<https://other.example/u>",)}),
    ]
    data = collect_unsub_targets(
        _UnsubMB(msgs), sender="news@", folder="INBOX", keep=["@bank.example"]
    )
    assert data["uids"] == ["2"]
    assert [a["target"] for a in data["actions"]] == ["https://other.example/u"]
    assert data["kept_count"] == 1


def test_unsubscribe_of_a_kept_sender_does_nothing(keep_env, monkeypatch):
    msgs = [_msg(1, "news@bank.example", {"List-Unsubscribe": ("<https://bank.example/u>",)})]
    mb = _MB(msgs)
    keep_env.use(mb, keep=["@bank.example"])
    called = []
    monkeypatch.setattr(ccli, "perform_unsubscribe", lambda a: called.append(a) or (True, "ok"))
    r = CliRunner().invoke(
        cli, ["unsubscribe", "--sender", "news@bank.example", "--apply", "--json"]
    )
    assert r.exit_code == 0, r.output
    out = json.loads(r.output)
    assert out["matching_count"] == 0
    assert out["kept_count"] == 1
    assert called == []
    assert mb.moves == []


def test_bounces_keep_senders_on_the_list():
    msgs = [
        _msg(1, "MAILER-DAEMON@mx.example", subject="Undelivered Mail Returned to Sender"),
        _msg(2, "boss@bank.example", subject="Out of office: back on Monday"),
    ]
    mb = _MB(msgs)
    res = run_bounces(mb, folder="INBOX", apply=True, keep=["@bank.example"])
    assert res.affected_uids == ["1"]
    assert res.kept_count == 1


def test_bad_keep_entry_written_by_hand_stops_before_connecting(tmp_path, monkeypatch):
    p = tmp_path / "cfg.json"
    monkeypatch.setenv(DEFAULT_CONFIG_PATH_ENV, str(p))
    monkeypatch.delenv("MAILBOX_CLEANUP_ACCOUNT", raising=False)
    p.write_text(json.dumps(_raw(["The Code"])), encoding="utf-8")
    connected = []
    monkeypatch.setattr(ccli, "imap_connect", lambda *a, **kw: connected.append(1))
    r = CliRunner().invoke(cli, ["delete", "--category", "newsletter", "--json"])
    assert r.exit_code == 4, r.output
    assert json.loads(r.output)["error_code"] == "bad_config"
    assert connected == []


def test_unsubscribe_of_a_kept_sender_says_so_and_logs_no_success(keep_env, monkeypatch):
    msgs = [_msg(1, "news@bank.example", {"List-Unsubscribe": ("<https://bank.example/u>",)})]
    keep_env.use(_MB(msgs), keep=["@bank.example"])
    monkeypatch.setattr(ccli, "perform_unsubscribe", lambda a: (True, "ok"))
    r = CliRunner().invoke(cli, ["unsubscribe", "--sender", "news@bank.example", "--apply"])
    assert r.exit_code == 0, r.output
    assert "Performed" not in r.output
    assert "keep list" in r.output
    (rec,) = [json.loads(x) for x in keep_env.log.read_text().splitlines()]
    assert rec["result"] == "kept"
    assert rec["affected_uids"] == []
    assert rec["keep_config_count"] == 1  # #75: the length, not the list


# --- #75: no filter value reaches the audit log ------------------------------------

_CANARY = "canary-7f3a@leak.example"
_CANARY_SUBJECT = "canary-subject-91c2"


@pytest.mark.parametrize(
    "argv",
    [
        ["delete", "--sender", _CANARY, "--subject-contains", _CANARY_SUBJECT],
        ["move", "--to", "Archiv", "--sender", _CANARY, "--subject-contains", _CANARY_SUBJECT],
    ],
    ids=["delete", "move"],
)
def test_cleanup_logs_no_filter_value(keep_env, argv):
    keep_env.use(_MB([_msg(1, _CANARY, subject=_CANARY_SUBJECT)]), keep=["@kept.example"])
    r = CliRunner().invoke(cli, [*argv, "--keep", "other@kept.example", "--apply", "--json"])
    assert r.exit_code == 0, r.output
    text = keep_env.log.read_text()
    assert text.strip(), "no audit record written — the check would be vacuous"
    for value in (_CANARY, _CANARY_SUBJECT, "kept.example"):
        assert value not in text
    (rec,) = [json.loads(x) for x in text.splitlines()]
    assert rec["arg_keys"] == ["keep", "sender", "subject_contains"]
    assert rec["affected_uids"] == ["1"]
    # where the mail went stays readable: a destination, not a filter value
    assert rec.get("target_folder") == ("Archiv" if argv[0] == "move" else None)


def test_unsubscribe_logs_no_sender(keep_env, monkeypatch):
    msgs = [_msg(1, _CANARY, {"List-Unsubscribe": ("<https://leak.example/u>",)})]
    keep_env.use(_MB(msgs), keep=[])
    monkeypatch.setattr(ccli, "perform_unsubscribe", lambda a: (True, "ok"))
    r = CliRunner().invoke(cli, ["unsubscribe", "--sender", _CANARY, "--apply"])
    assert r.exit_code == 0, r.output
    text = keep_env.log.read_text()
    assert text.strip(), "no audit record written — the check would be vacuous"
    assert "leak.example" not in text
    (rec,) = [json.loads(x) for x in text.splitlines()]
    assert rec["arg_keys"] == ["sender"]
    assert rec["keep_config_count"] == 0
