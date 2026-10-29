"""Guard library for the bot profile (#76 section 3, slice 3b).

The library is pure: no click, no config read, no profile read. These tests call it
directly, with no model and no host in the loop.
"""

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
