"""Guard library for the bot profile (#76 section 3, slice 3b).

The library is pure: no click, no config read, no profile read. These tests call it
directly, with no model and no host in the loop.
"""

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
