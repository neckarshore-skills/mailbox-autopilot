"""Guard library for the bot profile (#76 section 3, slice 3b).

The library is pure: no click, no config read, no profile read. These tests call it
directly, with no model and no host in the loop.
"""

import json as _json
import os
import stat
from types import SimpleNamespace

import pytest

from mailbox_cleanup import guards as g


def _refusal(fn, *a, **kw) -> g.GuardRefused:
    with pytest.raises(g.GuardRefused) as e:
        fn(*a, **kw)
    return e.value


# --- refusal shape -------------------------------------------------------------------


@pytest.mark.parametrize(
    "code, exit_code",
    [("profile_refused", 6), ("cap_reached", 7), ("preview_mismatch", 4)],
)
def test_each_error_code_has_its_exit_code(code, exit_code):
    assert g.GuardRefused(code, "r", "m").exit_code == exit_code


def test_an_unknown_error_code_is_a_programming_error():
    with pytest.raises(ValueError):
        g.GuardRefused("no_such_code", "r", "m")


def test_audit_fields_carry_code_rule_and_class_but_no_values():
    e = g.GuardRefused("cap_reached", "move_per_day", "a message", action="move")
    assert e.audit_fields() == {
        "error_code": "cap_reached",
        "rule": "move_per_day",
        "action": "move",
    }


# --- (h) UIDs: digits only, no ranges ------------------------------------------------


def test_valid_uids_are_canonical_deduplicated_and_sorted():
    assert g.check_uids(["12", "3", "12", "0007"]) == ("3", "7", "12")


@pytest.mark.parametrize(
    "bad",
    [
        "*",
        "1:*",
        "1:5",
        "3,4",
        " 3",
        "3 ",
        "-3",
        "+3",
        "3.0",
        "0",
        "00",
        str(2**32),
        "²",  # str.isdigit() accepts it
        "٣",  # Arabic-Indic three, also a Unicode digit
        "",
    ],
)
def test_a_uid_that_is_not_plain_digits_in_range_is_refused(bad):
    e = _refusal(g.check_uids, ["1", bad])
    assert (e.error_code, e.exit_code) == ("profile_refused", 6)
    assert e.rule == "uid_format"


@pytest.mark.parametrize("bad", [1, True, None, b"1", 1.0])
def test_a_uid_that_is_not_a_string_is_refused(bad):
    assert _refusal(g.check_uids, [bad]).rule == "uid_format"


@pytest.mark.parametrize("bad", ["1,2,3", b"1", None, 5])
def test_a_uid_set_that_is_not_a_list_is_refused(bad):
    assert _refusal(g.check_uids, bad).rule == "uid_set"


def test_an_empty_uid_set_is_refused():
    assert _refusal(g.check_uids, []).rule == "uid_empty"


def test_the_highest_uid_is_accepted():
    assert g.check_uids([str(2**32 - 1)]) == (str(2**32 - 1),)


# --- (b) flags: an allowlist ---------------------------------------------------------


def test_the_bot_may_add_flagged_seen_and_its_own_keywords():
    assert g.check_flags(add=["\\flagged", "\\Seen", "$BotDone"], remove=[]) == (
        ("\\Flagged", "\\Seen", "$BotDone"),
        (),
    )


def test_the_bot_may_remove_its_own_keywords():
    assert g.check_flags(add=[], remove=["$BotTodo"]) == ((), ("$BotTodo",))


@pytest.mark.parametrize(
    "flag",
    ["\\Deleted", "\\deleted", "\\DELETED", "\\Answered", "\\Draft", "\\Recent"],
)
def test_a_system_flag_outside_the_allowlist_is_refused(flag):
    assert _refusal(g.check_flags, add=[flag], remove=[]).rule == "flag_forbidden"


@pytest.mark.parametrize("flag", ["$Junk", "$junk", "Junk", "$NotJunk", "NonJunk", "$Phishing"])
def test_a_junk_keyword_or_any_foreign_keyword_is_refused(flag):
    assert _refusal(g.check_flags, add=[flag], remove=[]).rule in (
        "flag_forbidden",
        "flag_not_allowed",
    )


@pytest.mark.parametrize("flag", ["\\Flagged", "\\flagged", "\\Seen", "$NotBot", "Junk"])
def test_removing_anything_but_an_own_keyword_is_refused(flag):
    assert _refusal(g.check_flags, add=[], remove=[flag]).rule in (
        "flag_forbidden",
        "flag_not_allowed",
    )


@pytest.mark.parametrize(
    "flag",
    [
        "$BotX) (\\Deleted",  # would close the flag list and open a new one in STORE
        "$Bot X",
        "$Bot",
        '$Bot"',
        "$Bot\\",
        "$Bot%",
        "$Bot*",
        "$Bot\r\nA1 EXPUNGE",
        "$Botä",
        "$Bot" + "x" * 41,
    ],
)
def test_a_keyword_that_is_not_a_strict_atom_is_refused(flag):
    assert _refusal(g.check_flags, add=[flag], remove=[]).rule == "flag_not_allowed"


@pytest.mark.parametrize("bad", [None, 1, b"\\Seen"])
def test_a_flag_that_is_not_a_string_is_refused(bad):
    assert _refusal(g.check_flags, add=[bad], remove=[]).rule == "flag_not_allowed"


def test_a_flag_request_with_nothing_in_it_is_refused():
    assert _refusal(g.check_flags, add=[], remove=[]).rule == "flag_empty"


# --- (f) native MOVE only ------------------------------------------------------------


def test_native_move_passes():
    g.require_native_move(["IMAP4REV1", "move", "UIDPLUS"])


@pytest.mark.parametrize("caps", [["IMAP4REV1", "UIDPLUS"], [], ["MOVEX"]])
def test_a_server_without_native_move_is_refused_even_with_uidplus(caps):
    assert _refusal(g.require_native_move, caps).rule == "move_not_native"


# --- (a) (a2) (g) folders, resolved against LIST ----------------------------------


def _entry(name, *flags, delim="/"):
    return SimpleNamespace(name=name, delim=delim, flags=tuple(flags))


LISTING = [
    _entry("INBOX", "\\HasChildren"),
    _entry("Bot", "\\HasChildren"),
    _entry("Bot/Newsletter"),
    _entry("Bot/Papierkorb"),  # a localized trash name, no SPECIAL-USE flag
    _entry("Bot/Marked", "\\Flagged"),  # SPECIAL-USE inside the namespace
    _entry("Papierkorb"),  # created by a client, no flag
    _entry("Trash", "\\Trash"),
    _entry("Spam"),  # junk without a SPECIAL-USE flag
    _entry("Junk", "\\Junk"),
    _entry("Sent", "\\Sent"),
    _entry("Drafts", "\\Drafts"),
    _entry("All Mail", "\\All"),
    _entry("Projects"),
    _entry("Bots"),  # shares the prefix "Bot" but is outside the namespace
]


def _view(listing=LISTING, **kw):
    kw.setdefault("namespace", "Bot")
    return g.FolderView(listing, **kw)


def test_a_namespace_folder_is_a_valid_target():
    assert _view().check_target("Bot/Newsletter") == "Bot/Newsletter"
    assert _view().check_target("Bot") == "Bot"


@pytest.mark.parametrize(
    "name, rule",
    [
        ("Projects", "target_outside_namespace"),
        ("Bots", "target_outside_namespace"),
        ("bot/Newsletter", "folder_unknown"),  # names are case-sensitive, except INBOX
        ("INBOX", "target_outside_namespace"),
        ("Trash", "target_outside_namespace"),
        ("Papierkorb", "target_outside_namespace"),
        ("Bot/Papierkorb", "target_trash_like"),
        ("Bot/Marked", "target_special_use"),
        ("Bot/Missing", "folder_unknown"),
        ("Bot/*", "folder_name"),
        ("Bot/%", "folder_name"),
        ("Bot/x\r\nA1 DELETE INBOX", "folder_name"),
        ("", "folder_name"),
        ("B" * 201, "folder_name"),
    ],
)
def test_a_target_outside_the_allowlist_is_refused(name, rule):
    assert _refusal(_view().check_target, name).rule == rule


def test_an_owner_listed_target_is_valid_but_still_not_a_trash():
    v = _view(owner_targets=["Projects", "Papierkorb"])
    assert v.check_target("Projects") == "Projects"
    assert _refusal(v.check_target, "Papierkorb").rule == "target_trash_like"


@pytest.mark.parametrize("name", ["INBOX", "inbox", "Inbox", "Bot", "Bot/Newsletter"])
def test_inbox_and_the_namespace_are_valid_sources(name):
    assert _view().check_source(name) in ("INBOX", name)


@pytest.mark.parametrize(
    "name, rule",
    [
        ("Junk", "source_not_allowed"),
        ("Spam", "source_not_allowed"),
        ("Trash", "source_not_allowed"),
        ("Sent", "source_not_allowed"),
        ("Drafts", "source_not_allowed"),
        ("All Mail", "source_not_allowed"),
        ("Projects", "source_not_allowed"),
        ("Bot/Papierkorb", "source_forbidden"),
        ("Bot/Marked", "source_forbidden"),
    ],
)
def test_junk_trash_sent_drafts_all_and_foreign_folders_are_refused_as_sources(name, rule):
    assert _refusal(_view(owner_targets=["Projects"]).check_source, name).rule == rule


def test_a_junk_folder_inside_the_namespace_is_refused_as_a_source_by_flag_or_name():
    listing = [*LISTING, _entry("Bot/Junk", "\\Junk"), _entry("Bot/spam")]
    v = _view(listing)
    assert _refusal(v.check_source, "Bot/Junk").rule == "source_forbidden"
    assert _refusal(v.check_source, "Bot/spam").rule == "source_forbidden"


def test_a_new_folder_inside_the_namespace_is_allowed():
    assert _view().check_new_folder("Bot/Receipts") == "Bot/Receipts"
    assert _view().check_new_folder("Bot/Newsletter/2026") == "Bot/Newsletter/2026"


def test_the_namespace_itself_may_be_created_when_it_is_missing():
    v = _view([_entry("INBOX")])
    assert v.check_new_folder("Bot") == "Bot"


@pytest.mark.parametrize(
    "name, rule",
    [
        ("Receipts", "folder_outside_namespace"),
        ("Bots/x", "folder_outside_namespace"),
        ("INBOX/x", "folder_outside_namespace"),
        ("Bot/Newsletter", "folder_exists"),
        ("Bot/Papierkorb2/x", "folder_parent_missing"),
        ("Bot/Trash", "folder_trash_like"),
        ("Bot/Gelöschte Elemente", "folder_trash_like"),
        ("Bot/Deleted Items", "folder_trash_like"),
        ("Bot/Unerwünscht", "folder_trash_like"),
        ("Bot/BIN", "folder_trash_like"),
        ("Bot//x", "folder_name"),
        ("Bot/", "folder_name"),
        ("Bot/x*", "folder_name"),
    ],
)
def test_a_new_folder_outside_the_namespace_or_trash_like_is_refused(name, rule):
    assert _refusal(_view().check_new_folder, name).rule == rule


def test_with_a_dot_delimiter_the_namespace_uses_the_dot():
    listing = [_entry("INBOX", delim="."), _entry("INBOX.Bot", delim=".")]
    v = _view(listing, namespace="INBOX.Bot")
    assert v.check_new_folder("INBOX.Bot.Receipts") == "INBOX.Bot.Receipts"
    assert _refusal(v.check_new_folder, "INBOX.Trash").rule == "folder_outside_namespace"
    assert _refusal(v.check_new_folder, "INBOX.Bot/x").rule == "folder_outside_namespace"


@pytest.mark.parametrize(
    "namespace",
    ["", "Trash", "Papierkorb", "INBOX", "inbox", "Trash/Bot", "Bot*", "Junk", None],
)
def test_a_namespace_that_is_a_trash_inbox_nested_or_invalid_is_refused(namespace):
    assert _refusal(g.FolderView, LISTING, namespace=namespace).rule == "namespace_invalid"


def test_a_namespace_with_a_special_use_flag_is_refused():
    listing = [_entry("INBOX"), _entry("Bot", "\\Archive")]
    assert _refusal(g.FolderView, listing, namespace="Bot").rule == "namespace_invalid"


def test_the_host_sees_only_inbox_the_namespace_and_owner_targets():
    v = _view(owner_targets=["Projects", "Not-There"])
    assert v.visible() == (
        "Bot",
        "Bot/Marked",
        "Bot/Newsletter",
        "Bot/Papierkorb",
        "INBOX",
        "Projects",
    )


# --- (d) preview token ---------------------------------------------------------------


class _Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def _plan(**over):
    doc = dict(
        action="move",
        account="bot",
        source="INBOX",
        uidvalidity=42,
        uids=("3", "7"),
        target="Bot/Newsletter",
    )
    doc.update(over)
    return g.Plan(**doc)


def test_a_token_returns_exactly_the_previewed_plan():
    ledger = g.PreviewLedger()
    token = ledger.issue(_plan(uids=("7", "3", "7")))
    plan = ledger.redeem(token, action="move", account="bot")
    assert (plan.source, plan.target, plan.uids, plan.uidvalidity) == (
        "INBOX",
        "Bot/Newsletter",
        ("3", "7"),
        42,
    )


def test_the_apply_takes_no_uids_and_no_target():
    import inspect

    params = set(inspect.signature(g.PreviewLedger.redeem).parameters)
    assert params == {"self", "token", "action", "account"}


def test_the_same_count_with_a_different_uid_set_cannot_be_applied():
    """#76: --expect-count matches a number only, so a preview of 2 and an apply of 2
    different messages passed. A token names its own UIDs and nothing else."""
    ledger = g.PreviewLedger()
    token_a = ledger.issue(_plan(uids=("3", "7")))
    ledger.issue(_plan(uids=("4", "8")))
    assert ledger.redeem(token_a, action="move", account="bot").uids == ("3", "7")


def test_a_token_is_random_even_for_the_same_plan():
    ledger = g.PreviewLedger()
    a, b = ledger.issue(_plan()), ledger.issue(_plan())
    assert a != b and len(a) == 64 and int(a, 16) >= 0


def test_a_token_is_single_use():
    ledger = g.PreviewLedger()
    token = ledger.issue(_plan())
    ledger.redeem(token, action="move", account="bot")
    e = _refusal(ledger.redeem, token, action="move", account="bot")
    assert (e.error_code, e.exit_code, e.rule) == ("preview_mismatch", 4, "token_unknown")


def test_a_token_expires_and_is_spent_by_the_refusal():
    clock = _Clock()
    ledger = g.PreviewLedger(lifetime=300, clock=clock)
    token = ledger.issue(_plan())
    clock.t += 301
    assert _refusal(ledger.redeem, token, action="move", account="bot").rule == "token_expired"
    clock.t -= 301
    assert _refusal(ledger.redeem, token, action="move", account="bot").rule == "token_unknown"


def test_a_token_within_its_lifetime_is_accepted():
    clock = _Clock()
    ledger = g.PreviewLedger(lifetime=300, clock=clock)
    token = ledger.issue(_plan())
    clock.t += 300
    ledger.redeem(token, action="move", account="bot")


@pytest.mark.parametrize(
    "kw, rule",
    [
        ({"action": "flag", "account": "bot"}, "token_action"),
        ({"action": "move", "account": "other"}, "token_account"),
    ],
)
def test_a_token_for_another_action_or_account_is_refused_and_spent(kw, rule):
    ledger = g.PreviewLedger()
    token = ledger.issue(_plan())
    assert _refusal(ledger.redeem, token, **kw).rule == rule
    assert _refusal(ledger.redeem, token, action="move", account="bot").rule == "token_unknown"


@pytest.mark.parametrize("token", ["", "1:*", "A" * 64, "g" * 64, "0" * 63, 123, None])
def test_a_malformed_token_is_refused(token):
    e = _refusal(g.PreviewLedger().redeem, token, action="move", account="bot")
    assert (e.error_code, e.rule) == ("preview_mismatch", "token_invalid")


def test_an_unknown_well_formed_token_is_refused():
    e = _refusal(g.PreviewLedger().redeem, "0" * 64, action="move", account="bot")
    assert e.rule == "token_unknown"


@pytest.mark.parametrize(
    "over, rule",
    [
        ({"uids": ("1:*",)}, "uid_format"),
        ({"uids": ()}, "uid_empty"),
        ({"action": "delete"}, "plan_invalid"),
        ({"uidvalidity": 0}, "plan_invalid"),
        ({"uidvalidity": True}, "plan_invalid"),
        ({"uidvalidity": "42"}, "plan_invalid"),
        ({"target": None}, "plan_invalid"),  # a move needs a target
    ],
)
def test_an_invalid_plan_gets_no_token(over, rule):
    assert _refusal(g.PreviewLedger().issue, _plan(**over)).rule == rule


def test_a_flag_plan_needs_flags_and_no_target():
    ledger = g.PreviewLedger()
    token = ledger.issue(_plan(action="flag", target=None, flags_add=("\\Seen",)))
    assert ledger.redeem(token, action="flag", account="bot").flags_add == ("\\Seen",)
    assert _refusal(ledger.issue, _plan(action="flag", target=None)).rule == "plan_invalid"
    assert (
        _refusal(ledger.issue, _plan(action="flag", flags_add=("\\Seen",))).rule == "plan_invalid"
    )


def test_a_changed_uidvalidity_refuses_the_apply():
    """With a new UIDVALIDITY the same numbers name different messages."""
    g.check_uidvalidity(_plan(), 42)
    assert _refusal(g.check_uidvalidity, _plan(), 43).rule == "uidvalidity_changed"


def test_a_uid_that_left_the_source_refuses_the_whole_apply():
    g.check_uids_present(_plan(), ["3", "7", "9"])
    e = _refusal(g.check_uids_present, _plan(), ["3"])
    assert (e.error_code, e.rule) == ("preview_mismatch", "uids_gone")


def test_a_flag_plan_goes_through_the_flag_allowlist():
    plan = _plan(action="flag", target=None, flags_add=("\\Deleted",))
    assert _refusal(g.PreviewLedger().issue, plan).rule == "flag_forbidden"


# --- (d) caps: per run and rolling 24 hours ------------------------------------------


def _caps(
    tmp_path, *, per_run=None, per_day=None, clock=None, run_id="run-1", store=None, provision=True
):
    store = store or tmp_path / "caps.jsonl"
    if provision and store.parent.is_dir() and not os.path.lexists(store):
        g.CapLedger.provision(store)
    return g.CapLedger(
        per_run=per_run or dict(g.FIRST_ROLLOUT_PER_RUN),
        per_day=per_day or {c: 5 for c in g.ACTION_CLASSES},
        store=store,
        run_id=run_id,
        clock=clock or _Clock(),
    )


def test_the_first_rollout_caps_are_the_decided_ones():
    assert g.FIRST_ROLLOUT_PER_RUN == {"create_folder": 5, "draft": 5, "flag": 10, "move": 50}
    assert g.FIRST_ROLLOUT_PER_DAY == {"create_folder": 0, "draft": 0, "flag": 0, "move": 0}


@pytest.mark.parametrize("action", ["create_folder", "draft", "flag", "move"])
def test_a_daily_cap_of_zero_refuses_without_touching_the_store(tmp_path, action):
    store = tmp_path / "missing-dir" / "caps.jsonl"
    caps = _caps(tmp_path, per_day=dict(g.FIRST_ROLLOUT_PER_DAY), store=store)
    e = _refusal(caps.reserve, action, 1)
    assert (e.error_code, e.exit_code, e.rule) == ("cap_reached", 7, f"{action}_per_day")
    assert e.audit_fields() == {
        "error_code": "cap_reached",
        "rule": f"{action}_per_day",
        "action": action,
    }
    assert not store.parent.exists()


def test_the_per_run_cap_counts_within_the_run(tmp_path):
    caps = _caps(tmp_path, per_day={c: 1000 for c in g.ACTION_CLASSES})
    caps.reserve("move", 50)
    e = _refusal(caps.reserve, "move", 1)
    assert (e.error_code, e.rule, e.action) == ("cap_reached", "move_per_run", "move")
    caps.reserve("flag", 10)  # each action class is counted separately
    assert _refusal(caps.reserve, "flag", 1).rule == "flag_per_run"


def test_the_daily_cap_counts_across_runs(tmp_path):
    clock = _Clock()
    _caps(tmp_path, clock=clock, run_id="run-1").reserve("move", 3)
    clock.t += 3600
    second = _caps(tmp_path, clock=clock, run_id="run-2")
    assert _refusal(second.reserve, "move", 3).rule == "move_per_day"
    second.reserve("move", 2)
    assert _refusal(second.reserve, "move", 1).rule == "move_per_day"


def test_a_request_above_what_remains_is_refused_whole_and_not_recorded(tmp_path):
    caps = _caps(tmp_path)
    caps.reserve("move", 3)
    before = (tmp_path / "caps.jsonl").read_text()
    assert _refusal(caps.reserve, "move", 3).rule == "move_per_day"
    assert (tmp_path / "caps.jsonl").read_text() == before


def test_entries_older_than_24_hours_no_longer_count(tmp_path):
    clock = _Clock()
    _caps(tmp_path, clock=clock).reserve("move", 5)
    clock.t += g.DAY_SECONDS + 1
    _caps(tmp_path, clock=clock, run_id="run-2").reserve("move", 5)


def test_an_entry_from_the_future_still_counts(tmp_path):
    """A clock set back must not reopen the day."""
    clock = _Clock()
    _caps(tmp_path, clock=clock).reserve("move", 5)
    clock.t -= 3 * g.DAY_SECONDS
    assert _refusal(_caps(tmp_path, clock=clock).reserve, "move", 1).rule == "move_per_day"


def test_the_store_is_provisioned_owner_only_and_records_no_values(tmp_path):
    old = os.umask(0o022)
    try:
        _caps(tmp_path, run_id="run-7").reserve("move", 2)
    finally:
        os.umask(old)
    store = tmp_path / "caps.jsonl"
    assert stat.S_IMODE(store.stat().st_mode) == 0o600
    record = _json.loads(store.read_text().splitlines()[0])
    assert set(record) == {"ts", "action", "count", "run"}
    assert (record["action"], record["count"], record["run"]) == ("move", 2, "run-7")


@pytest.mark.parametrize(
    "content",
    [
        "not json\n",
        '{"ts": 1}\n',
        '{"ts": "x", "action": "move", "count": 1, "run": "r"}\n',
        "[1]\n",
    ],
)
def test_an_unreadable_store_refuses_when_the_cap_is_above_zero(tmp_path, content):
    store = tmp_path / "caps.jsonl"
    store.write_text(content)
    os.chmod(store, 0o600)
    e = _refusal(_caps(tmp_path).reserve, "move", 1)
    assert (e.error_code, e.rule) == ("profile_refused", "cap_store_unreadable")


def test_a_store_in_a_missing_directory_refuses_when_the_cap_is_above_zero(tmp_path):
    caps = _caps(tmp_path, store=tmp_path / "missing" / "caps.jsonl")
    assert _refusal(caps.reserve, "move", 1).rule == "cap_store_unreadable"


def test_a_symlinked_store_is_refused(tmp_path):
    real = tmp_path / "real.jsonl"
    real.write_text("")
    os.chmod(real, 0o600)
    (tmp_path / "caps.jsonl").symlink_to(real)
    assert _refusal(_caps(tmp_path).reserve, "move", 1).rule == "cap_store_insecure"


@pytest.mark.parametrize("mode", [0o620, 0o602, 0o666])
def test_a_store_others_can_write_is_refused(tmp_path, mode):
    store = tmp_path / "caps.jsonl"
    store.write_text("")
    os.chmod(store, mode)
    assert _refusal(_caps(tmp_path).reserve, "move", 1).rule == "cap_store_insecure"


@pytest.mark.parametrize(
    "per_run",
    [
        {"move": 50, "flag": 10, "create_folder": 5},  # a class is missing
        {"move": 50, "flag": 10, "create_folder": 5, "draft": 5, "delete": 1},
        {"move": -1, "flag": 10, "create_folder": 5, "draft": 5},
        {"move": True, "flag": 10, "create_folder": 5, "draft": 5},
        {"move": "50", "flag": 10, "create_folder": 5, "draft": 5},
    ],
)
def test_invalid_caps_refuse_everything(tmp_path, per_run):
    with pytest.raises(g.GuardRefused) as e:
        _caps(tmp_path, per_run=per_run)
    assert e.value.rule == "caps_invalid"


@pytest.mark.parametrize(
    "action, count", [("delete", 1), ("move", 0), ("move", -1), ("move", True)]
)
def test_an_unknown_action_or_a_bad_count_is_refused(tmp_path, action, count):
    assert _refusal(_caps(tmp_path).reserve, action, count).rule in ("action_unknown", "cap_count")


# --- security review of 3b: name normalisation and the token lock --------------------


@pytest.mark.parametrize(
    "name, rule",
    [
        ("Bot/Trash ", "folder_name"),  # an edge space would hide the trash name
        ("Bot/ Trash", "folder_name"),
        ("Bot/Ｔｒａｓｈ", "folder_trash_like"),  # fullwidth letters, NFKC folds them
        ("Bot/Deleted  Items", "folder_trash_like"),  # a run of spaces
        ("Bot/ＳＰＡＭ", "folder_trash_like"),
    ],
)
def test_a_trash_name_cannot_hide_behind_spaces_or_compatibility_forms(name, rule):
    assert _refusal(_view().check_new_folder, name).rule == rule


def test_an_existing_folder_with_an_edge_space_is_no_target():
    v = _view([*LISTING, _entry("Bot/Trash ")])
    assert _refusal(v.check_target, "Bot/Trash ").rule == "folder_name"


class _RecordingLock:
    def __init__(self):
        self.entered = 0

    def __enter__(self):
        self.entered += 1

    def __exit__(self, *exc):
        return False


def test_issue_and_redeem_hold_the_ledger_lock():
    """A concurrent issue rebuilt the plan table from a snapshot and could bring back a
    token another thread had just redeemed. Both paths now run under one lock."""
    ledger = g.PreviewLedger()
    lock = _RecordingLock()
    ledger._lock = lock
    token = ledger.issue(_plan())
    ledger.redeem(token, action="move", account="bot")
    assert lock.entered == 2


def test_tokens_redeemed_from_many_threads_are_spent_once():
    import threading

    ledger = g.PreviewLedger()
    tokens = [ledger.issue(_plan()) for _ in range(200)]
    wins = []

    def worker():
        for t in tokens:
            try:
                ledger.redeem(t, action="move", account="bot")
                wins.append(t)
            except g.GuardRefused:
                pass
            ledger.issue(_plan())

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert sorted(wins) == sorted(tokens)


def test_inbox_is_case_insensitive_but_not_a_fullwidth_form():
    assert _view().check_source("iNbOx") == "INBOX"
    assert _refusal(_view().check_source, "ＩＮＢＯＸ").rule == "folder_unknown"


def test_reserve_never_creates_the_store(tmp_path):
    """Deleting the store must not reset the day (security review of 3b)."""
    caps = _caps(tmp_path, provision=False)
    assert _refusal(caps.reserve, "move", 1).rule == "cap_store_unreadable"
    assert not (tmp_path / "caps.jsonl").exists()


def test_a_deleted_store_refuses_instead_of_starting_a_new_day(tmp_path):
    _caps(tmp_path).reserve("move", 5)
    (tmp_path / "caps.jsonl").unlink()
    later = _caps(tmp_path, run_id="run-2", provision=False)
    assert _refusal(later.reserve, "move", 1).rule == "cap_store_unreadable"


def test_provision_refuses_an_existing_store(tmp_path):
    g.CapLedger.provision(tmp_path / "caps.jsonl")
    with pytest.raises(FileExistsError):
        g.CapLedger.provision(tmp_path / "caps.jsonl")


def test_a_hard_linked_store_is_refused(tmp_path):
    _caps(tmp_path)
    os.link(tmp_path / "caps.jsonl", tmp_path / "other.jsonl")
    assert _refusal(_caps(tmp_path).reserve, "move", 1).rule == "cap_store_insecure"


def test_a_store_in_a_directory_others_can_write_is_refused(tmp_path):
    d = tmp_path / "shared"
    d.mkdir()
    g.CapLedger.provision(d / "caps.jsonl")
    os.chmod(d, 0o777)
    try:
        caps = _caps(tmp_path, store=d / "caps.jsonl")
        assert _refusal(caps.reserve, "move", 1).rule == "cap_store_insecure"
    finally:
        os.chmod(d, 0o755)


# --- (c) keep list at the last step ---------------------------------------------------

KEEP = ["boss@company.example", "@bank.example"]


def test_no_kept_sender_passes():
    g.ensure_none_kept(("3", "7"), {"3": "a@news.example", "7": "b@shop.example"}, KEEP)


@pytest.mark.parametrize(
    "sender",
    ["boss@company.example", "BOSS@Company.Example", "alerts@bank.example", "x@mail.bank.example"],
)
def test_a_kept_sender_refuses_the_whole_request_without_naming_it(sender):
    e = _refusal(g.ensure_none_kept, ("3", "7"), {"3": "a@news.example", "7": sender}, KEEP)
    assert (e.error_code, e.rule) == ("profile_refused", "keep_list")
    assert "example" not in str(e) and "example" not in repr(e.audit_fields())


@pytest.mark.parametrize(
    "senders",
    [
        {"3": "a@news.example"},
        {"3": "a@news.example", "7": None},
        {"3": "a@news.example", "7": ""},
        {"3": "a@news.example", "7": "undisclosed"},
    ],
)
def test_a_uid_whose_sender_cannot_be_read_is_refused(senders):
    assert _refusal(g.ensure_none_kept, ("3", "7"), senders, KEEP).rule == "keep_unverifiable"


def test_an_empty_keep_list_still_needs_a_readable_sender():
    assert _refusal(g.ensure_none_kept, ("3",), {"3": None}, []).rule == "keep_unverifiable"


def test_an_invalid_keep_entry_refuses():
    assert (
        _refusal(g.ensure_none_kept, ("3",), {"3": "a@b.example"}, ["boss"]).rule == "keep_invalid"
    )


def test_a_store_below_an_ancestor_others_can_write_is_refused(tmp_path):
    """Write access on any ancestor lets someone swap the path below it."""
    top = tmp_path / "open"
    (top / "inner").mkdir(parents=True)
    g.CapLedger.provision(top / "inner" / "caps.jsonl")
    os.chmod(top, 0o777)
    try:
        caps = _caps(tmp_path, store=top / "inner" / "caps.jsonl")
        assert _refusal(caps.reserve, "move", 1).rule == "cap_store_insecure"
    finally:
        os.chmod(top, 0o755)


def test_a_store_reached_through_a_symlinked_directory_is_checked_where_it_resolves(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    g.CapLedger.provision(real / "caps.jsonl")
    (tmp_path / "link").symlink_to(real)
    _caps(tmp_path, store=tmp_path / "link" / "caps.jsonl").reserve("move", 1)
    os.chmod(real, 0o777)
    try:
        caps = _caps(tmp_path, store=tmp_path / "link" / "caps.jsonl")
        assert _refusal(caps.reserve, "move", 1).rule == "cap_store_insecure"
    finally:
        os.chmod(real, 0o755)
