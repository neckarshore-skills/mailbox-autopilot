"""#76 slice 3a: the root-owned profile anchor, the bot's identity, and the CLI closed to the
bot before click parses anything. No server, no root: the anchor lives under tmp_path and
the owner uid it must have is the test user's (module attributes, never env)."""

import json
import os
import pwd
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from mailbox_cleanup import profile
from mailbox_cleanup.cli import cli

ME = pwd.getpwuid(os.getuid()).pw_name
OTHER = "nobody"  # exists on macOS and Linux
GID = 4242424  # a gid this process is not in


def _valid(**over):
    doc = {"schema_version": 1, "bot_user": ME, "account": "bot", "read_level": "lean"}
    doc.update(over)
    return doc


@pytest.fixture
def anchor(tmp_path, monkeypatch):
    """A secure anchor tree under tmp_path; `write` puts a profile document in it."""
    parent = tmp_path / "sys"
    parent.mkdir()
    os.chmod(parent, 0o755)
    adir = parent / "mailbox-autopilot"
    monkeypatch.setattr(profile, "ANCHOR_DIR", adir)
    monkeypatch.setattr(profile, "ANCHOR_OWNER_UID", os.getuid())

    def write(doc=None, raw=None, mode=0o644):
        adir.mkdir(exist_ok=True)
        os.chmod(adir, 0o755)
        f = adir / profile.PROFILE_NAME
        f.write_text(raw if raw is not None else json.dumps(doc or _valid()), encoding="utf-8")
        os.chmod(f, mode)
        return f

    return SimpleNamespace(parent=parent, dir=adir, write=write)


@pytest.fixture
def group(monkeypatch):
    """Simulate the root-created bot group: `members` by name, `process` = this process
    carries the group's gid."""

    def setup(*, exists=True, members=(), process=False):
        monkeypatch.setattr(
            profile, "_bot_group", lambda: (GID, frozenset(members)) if exists else None
        )
        monkeypatch.setattr(profile, "_caller_gids", lambda: {GID} if process else {1, 2})

    setup(exists=False)
    return setup


def _rule(fn, *a):
    with pytest.raises(profile.ProfileRefused) as e:
        fn(*a)
    return e.value.rule


# --- load_profile -------------------------------------------------------------


def test_a_valid_profile_loads(anchor):
    f = anchor.write()
    p = profile.load_profile()
    assert (p.bot_user, p.bot_uid, p.account, p.read_level) == (ME, os.getuid(), "bot", "lean")
    import hashlib

    assert p.sha256 == hashlib.sha256(f.read_bytes()).hexdigest()


def test_no_anchor_directory_is_not_installed(anchor):
    assert _rule(profile.load_profile) == "profile_not_installed"


def test_an_anchor_without_the_file_is_missing(anchor):
    anchor.dir.mkdir()
    os.chmod(anchor.dir, 0o755)
    assert _rule(profile.load_profile) == "profile_missing"


@pytest.mark.parametrize(
    "raw",
    [
        "not json",
        "[]",
        json.dumps({k: v for k, v in _valid().items() if k != "account"}),
        json.dumps(_valid(extra=1)),
        json.dumps(_valid(schema_version=2)),
        json.dumps(_valid(schema_version=True)),
        json.dumps(_valid(bot_user="")),
        json.dumps(_valid(bot_user="no-such-user-4242")),
        json.dumps(_valid(bot_user="root")),
        json.dumps(_valid(account="")),
        json.dumps(_valid(account="bot\r\nx")),
        json.dumps(_valid(read_level="full")),
        json.dumps(_valid(read_level=[])),
        json.dumps(_valid(read_level={})),
        json.dumps(_valid(account="b" * (profile.MAX_PROFILE_BYTES + 1))),
    ],
    ids=[
        "not-json",
        "not-an-object",
        "missing-key",
        "unknown-key",
        "future-schema",
        "bool-schema",
        "empty-user",
        "unknown-user",
        "root-user",
        "empty-account",
        "control-char-account",
        "full-read",
        "list-read-level",
        "object-read-level",
        "oversize",
    ],
)
def test_an_invalid_profile_is_refused(anchor, raw):
    anchor.write(raw=raw)
    assert _rule(profile.load_profile) == "profile_invalid"


@pytest.mark.parametrize("mode", [0o664, 0o646, 0o666])
def test_a_writable_profile_file_is_insecure(anchor, mode):
    anchor.write(mode=mode)
    assert _rule(profile.load_profile) == "profile_insecure"


def test_a_symlinked_profile_file_is_insecure(anchor, tmp_path):
    real = tmp_path / "elsewhere.json"
    real.write_text(json.dumps(_valid()), encoding="utf-8")
    anchor.dir.mkdir()
    os.chmod(anchor.dir, 0o755)
    (anchor.dir / profile.PROFILE_NAME).symlink_to(real)
    assert _rule(profile.load_profile) == "profile_insecure"


def test_the_opened_file_is_checked_again(anchor, monkeypatch):
    """lstat and open are two steps. If the file changes between them, what is hashed
    and parsed is not the file that was checked; fstat on the opened descriptor closes
    that window (James, #89 finding 2). The change is simulated inside os.open."""
    f = anchor.write()
    real_open = os.open

    def open_after_a_swap(path, flags, *a, **kw):
        if Path(path) == f:
            os.chmod(f, 0o666)
        return real_open(path, flags, *a, **kw)

    monkeypatch.setattr(profile.os, "open", open_after_a_swap)
    assert _rule(profile.load_profile) == "profile_insecure"


def test_a_group_writable_anchor_directory_is_insecure(anchor):
    anchor.write()
    os.chmod(anchor.dir, 0o775)
    assert _rule(profile.load_profile) == "anchor_insecure"


def test_a_writable_anchor_parent_is_insecure(anchor):
    anchor.write()
    os.chmod(anchor.parent, 0o777)
    assert _rule(profile.load_profile) == "anchor_insecure"


def test_a_symlinked_anchor_directory_is_insecure(anchor, tmp_path, monkeypatch):
    anchor.write()
    link = anchor.parent / "linked"
    link.symlink_to(anchor.dir)
    monkeypatch.setattr(profile, "ANCHOR_DIR", link)
    assert _rule(profile.load_profile) == "anchor_insecure"


def test_an_anchor_owned_by_someone_else_is_insecure(anchor, monkeypatch):
    anchor.write()
    monkeypatch.setattr(profile, "ANCHOR_OWNER_UID", os.getuid() + 1)
    assert _rule(profile.load_profile) == "anchor_insecure"


@pytest.mark.skipif(os.getuid() == 0, reason="root reads a 0000 file")
def test_an_unreadable_profile_is_refused(anchor):
    anchor.write(mode=0o000)
    assert _rule(profile.load_profile) == "profile_unreadable"


# --- the CLI gate ---------------------------------------------------------------


def test_nothing_installed_lets_the_cli_run(anchor, group):
    profile.cli_gate(os.getuid())  # no raise


@pytest.mark.parametrize("how", ["member-list", "process-gid"])
def test_a_bot_group_member_is_refused_even_without_any_profile(anchor, group, how):
    group(members=(ME,) if how == "member-list" else (), process=how == "process-gid")
    assert _rule(profile.cli_gate, os.getuid()) == "cli_closed_to_bot"


def test_a_non_member_runs_next_to_a_valid_profile_for_another_user(anchor, group):
    group(members=(OTHER,))
    anchor.write(_valid(bot_user=OTHER))
    profile.cli_gate(os.getuid())  # no raise


@pytest.mark.parametrize(
    "raw", ["not json", json.dumps(_valid(read_level=[]))], ids=["not-json", "list-read-level"]
)
def test_a_broken_profile_does_not_lock_out_a_non_member(anchor, group, raw):
    """An unhashable read_level raised TypeError, which the gate turned into gate_error
    for everyone, the owner included (CodeRabbit and James, #89 finding 1)."""
    group(members=(OTHER,))
    anchor.write(raw=raw)
    profile.cli_gate(os.getuid())  # no raise: the group, not the file, says who the bot is


def test_an_anchor_without_the_bot_group_refuses_everyone(anchor, group):
    group(exists=False)
    anchor.write(_valid(bot_user=OTHER))
    assert _rule(profile.cli_gate, os.getuid()) == "bot_group_missing"


def test_a_profile_naming_the_caller_refuses_even_outside_the_group(anchor, group):
    group(members=(OTHER,))
    anchor.write(_valid(bot_user=ME))
    assert _rule(profile.cli_gate, os.getuid()) == "bot_not_in_group"


def test_the_real_group_lookup_recognises_a_member_by_process_gid(anchor, monkeypatch):
    import grp

    monkeypatch.setattr(profile, "BOT_GROUP", grp.getgrgid(os.getgid()).gr_name)
    assert _rule(profile.cli_gate, os.getuid()) == "cli_closed_to_bot"


def test_the_real_group_lookup_of_an_absent_group_lets_the_cli_run(anchor):
    assert profile._bot_group() is None  # the autouse fixture names a group that does not exist
    profile.cli_gate(os.getuid())


# --- the host's entry: no unrestricted mode ------------------------------------


def test_the_host_gets_the_profile_for_the_bot(anchor, group):
    group(members=(ME,))
    anchor.write()
    assert profile.host_profile(os.getuid()).bot_user == ME


@pytest.mark.parametrize(
    "setup, rule",
    [
        (dict(group=dict(exists=False), doc=_valid()), "bot_group_missing"),
        (dict(group=dict(members=(OTHER,)), doc=_valid()), "not_bot_user"),
        (dict(group=dict(members=(ME,)), doc=None), "profile_not_installed"),
        (dict(group=dict(members=(ME,)), doc=_valid(bot_user=OTHER)), "profile_names_other_user"),
        (dict(group=dict(members=(ME,)), raw="not json"), "profile_invalid"),
    ],
    ids=["no-group", "not-member", "no-profile", "other-user", "broken"],
)
def test_the_host_refuses_every_other_state(anchor, group, setup, rule):
    group(**setup["group"])
    if setup.get("doc"):
        anchor.write(setup["doc"])
    if setup.get("raw"):
        anchor.write(raw=setup["raw"])
    assert _rule(profile.host_profile, os.getuid()) == rule


# --- CLI: refused before click parses -------------------------------------------


@pytest.fixture
def bot(anchor, group, tmp_path, monkeypatch):
    """This process is the bot: in the group, with a valid profile. Its pwd home is
    tmp_path/bothome; HOME and the audit override point somewhere else on purpose."""
    group(members=(ME,))
    anchor.write()
    home = tmp_path / "bothome"
    home.mkdir()
    monkeypatch.setattr(profile, "_home_of", lambda uid: home)
    monkeypatch.setenv("HOME", str(tmp_path / "envhome"))
    monkeypatch.setenv("MAILBOX_CLEANUP_AUDIT_LOG", str(tmp_path / "override.log"))
    monkeypatch.setenv("MAILBOX_CLEANUP_CONFIG", str(tmp_path / "other-config.json"))
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    return SimpleNamespace(log=home / ".mailbox-cleanup" / "audit.log", tmp=tmp_path)


ARGVS = {
    "delete-garbage": (["delete", "--garbage"], "delete"),
    "unknown-command": (["nosuchcmd"], "unknown"),
    "unknown-group-option": (["--bogus"], "unknown"),
    "manage-search": (["manage", "search", "--from", "jane@example.com"], "manage search"),
    "auth-set": (["auth", "set"], "auth set"),
    "config-show": (["config", "show"], "config show"),
    "version": (["--version"], "unknown"),
    "help": (["--help"], "unknown"),
}


@pytest.mark.parametrize("name", sorted(ARGVS))
def test_the_bot_is_refused_before_click_parses(bot, name):
    argv, command = ARGVS[name]
    res = CliRunner().invoke(cli, argv)
    assert res.exit_code == profile.REFUSED_EXIT, res.output
    out = json.loads(res.stdout)
    assert (out["ok"], out["error_code"], out["rule"]) == (
        False,
        "profile_refused",
        "cli_closed_to_bot",
    )
    assert "Usage" not in res.output
    (rec,) = [json.loads(line) for line in bot.log.read_text(encoding="utf-8").splitlines()]
    assert rec["subcommand"] == command
    assert (rec["result"], rec["error_code"], rec["rule"]) == (
        "refused",
        "profile_refused",
        "cli_closed_to_bot",
    )
    assert rec["caller_uid"] == os.getuid()
    assert len(rec["profile_sha256"]) == 64
    text = bot.log.read_text(encoding="utf-8")
    assert "jane@example.com" not in text and "--garbage" not in text and "--from" not in text


def test_the_refusal_record_ignores_home_and_the_audit_override(bot):
    CliRunner().invoke(cli, ["delete", "--garbage"])
    assert bot.log.is_file()
    assert not (bot.tmp / "override.log").exists()
    assert not (bot.tmp / "envhome").exists()
    assert stat.S_IMODE(bot.log.stat().st_mode) == 0o600
    assert stat.S_IMODE(bot.log.parent.stat().st_mode) == 0o700


def test_the_refusal_does_not_depend_on_the_audit_write(bot, tmp_path, monkeypatch):
    blocked = tmp_path / "blockedhome"
    blocked.mkdir()
    (blocked / ".mailbox-cleanup").write_text("a file where the directory should be")
    monkeypatch.setattr(profile, "_home_of", lambda uid: blocked)
    res = CliRunner().invoke(cli, ["delete", "--garbage"])
    assert res.exit_code == profile.REFUSED_EXIT
    assert json.loads(res.stdout)["error_code"] == "profile_refused"
    assert "could not write the refusal to the audit log" in res.stderr


def test_a_failing_gate_refuses_rather_than_lets_through(anchor, group, tmp_path, monkeypatch):
    def boom():
        raise OSError("directory service unavailable")

    monkeypatch.setattr(profile, "_bot_group", boom)
    monkeypatch.setattr(profile, "_home_of", lambda uid: tmp_path)
    res = CliRunner().invoke(cli, ["delete", "--garbage"])
    assert res.exit_code == profile.REFUSED_EXIT
    assert json.loads(res.stdout)["rule"] == "gate_error"


def test_without_an_installed_profile_the_cli_is_unchanged(anchor, group):
    res = CliRunner().invoke(cli, ["delete", "--json"])
    assert res.exit_code == 4, res.output  # the ordinary "at least one filter" answer
    assert json.loads(res.stdout)["error_code"] == "bad_args"
