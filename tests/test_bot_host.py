"""#76 slice 3c-1: the bot's MCP host. It serves only the bot, only with a valid profile
and a usable audit log, and it refuses every tool the profile does not offer before it
looks at an argument (section 5.7). No root and no server: the anchor lives under
tmp_path, the owner uid it must have is the test user's (module attributes, never env),
and the system append-only flag, which only root can set, is stood in for."""

import io
import json
import os
import pwd
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from mailbox_cleanup import guards, host, profile
from mailbox_cleanup.guards import GuardRefused

ME = pwd.getpwuid(os.getuid()).pw_name
GID = 4242424


def _caps(n):
    return {c: n for c in guards.ACTION_CLASSES}


def _profile_doc(**over):
    doc = {
        "schema_version": 2,
        "bot_user": ME,
        "account": "bot",
        "email": "bot@example.org",
        "server": "imap.example.org",
        "read_level": "lean",
        "namespace": "Bot",
        "owner_targets": [],
        "caps": {"per_run": _caps(5), "per_day": _caps(0)},
        "keep": [],
    }
    doc.update(over)
    return doc


@pytest.fixture
def install(tmp_path, monkeypatch):
    """A complete install under tmp_path: root-only anchor, profile, the bot group with
    this process as its member, and a provisioned host audit log."""
    root = tmp_path.resolve()
    parent = root / "sys"
    parent.mkdir()
    adir = parent / "mailbox-autopilot"
    adir.mkdir()
    for d in (parent, adir):
        os.chmod(d, 0o755)
    monkeypatch.setattr(profile, "ANCHOR_DIR", adir)
    monkeypatch.setattr(profile, "ANCHOR_OWNER_UID", os.getuid())
    monkeypatch.setattr(profile, "_bot_group", lambda: (GID, frozenset({ME})))
    monkeypatch.setattr(guards, "_is_append_only", lambda st: True)
    (adir / profile.PROFILE_NAME).write_text(json.dumps(_profile_doc()), encoding="utf-8")
    os.chmod(adir / profile.PROFILE_NAME, 0o644)
    log = adir / host.HOST_AUDIT_NAME
    guards.CapLedger.provision(log)
    return log


def _records(log: Path) -> list[dict]:
    return [json.loads(x) for x in log.read_text(encoding="utf-8").splitlines()]


def _rpc(*msgs) -> bytes:
    return b"".join(json.dumps(m).encode() + b"\n" for m in msgs)


INIT = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {"protocolVersion": "2025-06-18", "capabilities": {}},
}


def _call(name, arguments=None, id=2):
    params = {"name": name}
    if arguments is not None:
        params["arguments"] = arguments
    return {"jsonrpc": "2.0", "id": id, "method": "tools/call", "params": params}


def _serve(*msgs, uid=None):
    out = io.BytesIO()
    rc = host.run(io.BytesIO(_rpc(INIT, *msgs)), out, uid=os.getuid() if uid is None else uid)
    answers = {a["id"]: a for a in (json.loads(x) for x in out.getvalue().splitlines())}
    return rc, answers


def _payload(answer) -> dict:
    return json.loads(answer["result"]["content"][0]["text"])


# --- start: fail closed ----------------------------------------------------------------


def test_the_host_serves_the_bot_and_records_its_start(install):
    rc, answers = _serve({"jsonrpc": "2.0", "id": 3, "method": "tools/list"})
    assert rc == 0
    assert answers[3]["result"] == {"tools": []}  # 3c-1 builds no tool yet
    start = _records(install)[0]
    assert start["event"] == "start" and start["result"] == "ok"
    assert start["caller_uid"] == os.getuid() and start["profile"] is True
    assert start["profile_sha256"] == profile.load_profile().sha256
    assert len(start["run_id"]) == 16


def test_no_bot_group_means_no_service(install, monkeypatch, capsys):
    monkeypatch.setattr(profile, "_bot_group", lambda: None)
    rc, answers = _serve({"jsonrpc": "2.0", "id": 3, "method": "tools/list"})
    assert (rc, answers) == (6, {})
    assert "bot_group_missing" in capsys.readouterr().err


def test_a_caller_outside_the_group_is_not_served(install, monkeypatch):
    monkeypatch.setattr(profile, "_bot_group", lambda: (GID, frozenset({"someone-else"})))
    monkeypatch.setattr(profile, "_caller_gids", lambda: {1, 2})
    rc, answers = _serve()
    assert (rc, answers) == (6, {})
    refused = _records(install)[-1]
    assert (refused["event"], refused["result"], refused["rule"]) == (
        "start",
        "refused",
        "not_bot_user",
    )
    assert refused["profile"] is False


def test_a_missing_profile_means_no_service_and_is_audited(install):
    (profile.ANCHOR_DIR / profile.PROFILE_NAME).unlink()
    rc, answers = _serve()
    assert (rc, answers) == (6, {})
    assert _records(install)[-1]["rule"] == "profile_missing"


def test_an_invalid_profile_means_no_service(install):
    f = profile.ANCHOR_DIR / profile.PROFILE_NAME
    f.write_text(json.dumps(_profile_doc(server="localhost")), encoding="utf-8")
    assert _serve()[0] == 6


def test_a_missing_audit_log_means_no_service(install, capsys):
    install.unlink()
    rc, answers = _serve({"jsonrpc": "2.0", "id": 3, "method": "tools/list"})
    assert (rc, answers) == (6, {})
    assert "audit_log_missing" in capsys.readouterr().err


def test_an_audit_log_without_the_system_flag_means_no_service(install, monkeypatch):
    monkeypatch.setattr(guards, "_is_append_only", lambda st: False)
    assert _serve()[0] == 6


def test_a_symlink_in_the_audit_log_path_means_no_service(install, monkeypatch, tmp_path, capsys):
    """The anchor check is stood in for, so the refusal must come from the audit path
    check (James on #91: no link in any directory of a configured path)."""
    link = tmp_path.resolve() / "link"
    link.symlink_to(profile.ANCHOR_DIR.parent)
    monkeypatch.setattr(profile, "ANCHOR_DIR", link / profile.ANCHOR_DIR.name)
    monkeypatch.setattr(profile, "_secure_dir", lambda p: True)
    assert _serve()[0] == 6
    assert "audit_log_path_symlink" in capsys.readouterr().err


def test_the_audit_log_cannot_be_redirected_by_env(install, monkeypatch, tmp_path):
    other = tmp_path / "elsewhere.log"
    monkeypatch.setenv("MAILBOX_CLEANUP_AUDIT_LOG", str(other))
    monkeypatch.setenv("HOME", str(tmp_path))
    _serve(_call("delete"))
    assert not other.exists() and not (tmp_path / ".mailbox-cleanup").exists()
    assert [r["event"] for r in _records(install)] == ["start", "call", "stop"]


# --- the refusal path: the profile check comes before any argument --------------------


def test_a_tool_the_profile_does_not_offer_is_refused_and_audited(install):
    rc, answers = _serve(_call("delete", {"--anything": "x", "uids": "1:*"}))
    assert answers[2]["result"]["isError"] is True
    assert _payload(answers[2]) == {
        "error_code": "profile_refused",
        "rule": "tool_not_allowed",
        "exit_code": 6,
        "message": "the bot profile does not offer this tool",
    }
    call = _records(install)[1]
    assert {k: call[k] for k in ("event", "tool", "result", "error_code", "rule")} == {
        "event": "call",
        "tool": "delete",
        "result": "refused",
        "error_code": "profile_refused",
        "rule": "tool_not_allowed",
    }


def test_garbage_arguments_do_not_change_the_refusal(install):
    """A disabled command with garbage arguments is profile_refused, not a usage error
    (section 5.7): the arguments are never looked at."""
    for args in ("not an object", [1, 2], {"\x00": None}, None):
        _, answers = _serve(_call("delete", args))
        assert _payload(answers[2])["rule"] == "tool_not_allowed"


def test_no_argument_value_reaches_the_audit_log(install):
    _serve(_call("delete", {"folder": "Geheim-Projekt", "sender": "boss@example.org"}))
    text = install.read_text(encoding="utf-8")
    assert "Geheim-Projekt" not in text and "boss@example.org" not in text
    assert "arguments" not in _records(install)[1]


@pytest.mark.parametrize("name", ["evil\nname", "x" * 5000, 42, None, ["delete"], "DELETE"])
def test_an_unknown_tool_name_is_recorded_as_unknown(install, name):
    """The name is model-chosen text. Only names this program knows are recorded as
    they are, so the log cannot be filled with whatever the mail said."""
    _, answers = _serve(_call(name))
    assert _payload(answers[2])["rule"] == "tool_not_allowed"
    assert _records(install)[1]["tool"] == "unknown"


def test_every_excluded_command_is_refused_under_its_own_name(install):
    names = sorted(host.EXCLUDED_TOOLS)
    _serve(*(_call(n, id=10 + i) for i, n in enumerate(names)))
    calls = [r for r in _records(install) if r["event"] == "call"]
    assert [r["tool"] for r in calls] == names
    assert {r["rule"] for r in calls} == {"tool_not_allowed"}


def test_a_refusal_stands_when_its_audit_line_cannot_be_written(install, monkeypatch):
    real = guards.open_append_only

    def failing(path, **kw):
        if kw.get("prefix") == "audit_log" and getattr(failing, "armed", False):
            raise GuardRefused("profile_refused", "audit_log_missing", "gone")
        return real(path, **kw)

    monkeypatch.setattr(host, "open_append_only", failing)
    h = host.Host(profile=profile.load_profile(), audit=_audit(install), tools={})
    failing.armed = True
    result = h.call("delete", {})
    assert result["isError"] is True
    assert json.loads(result["content"][0]["text"])["rule"] == "tool_not_allowed"


# --- the call path for an offered tool (no real tool yet: a stand-in) -----------------


class FakeTool:
    def __init__(self, run):
        self.spec = {"name": "search", "description": "stand-in", "inputSchema": {"type": "object"}}
        self.run = run


def _audit(log):
    return host.HostAudit(log, run_id="r" * 16, caller_uid=os.getuid(), profile_sha256="s")


def _host(install, run):
    return host.Host(
        profile=profile.load_profile(), audit=_audit(install), tools={"search": FakeTool(run)}
    )


def test_an_offered_tool_runs_and_is_audited(install):
    h = _host(install, lambda args: {"found": 2})
    assert h.list_tools() == [h.tools["search"].spec]
    result = h.call("search", {"since": "2026-10-01"})
    assert result == {"content": [{"type": "text", "text": '{"found":2}'}], "isError": False}
    rec = _records(install)[-1]
    assert (rec["tool"], rec["result"]) == ("search", "ok")


def test_no_argument_value_reaches_the_audit_log_on_the_ok_path(install):
    """From slice 3c-2 real folder names and senders arrive as arguments."""
    _host(install, lambda args: {"found": 2}).call(
        "search", {"since": "2026-10-01", "folder": "Geheim-Projekt"}
    )
    text = install.read_text(encoding="utf-8")
    assert "Geheim-Projekt" not in text and "2026-10-01" not in text
    assert "arguments" not in _records(install)[-1]


def test_the_allowlist_carries_the_tickets_names_with_move_and_flag_split():
    """#76 section 2.2 by name; move and flag are each a preview and an apply, because
    the apply takes no UIDs. Acceptance 5b pins this list."""
    assert host.ALLOWED_TOOLS == {
        "create-folder",
        "draft",
        "flag-apply",
        "flag-preview",
        "move-apply",
        "move-preview",
        "read",
        "search",
        "thread",
    }


def test_a_tool_outside_the_allowlist_cannot_be_installed(install):
    with pytest.raises(ValueError):
        host.Host(
            profile=profile.load_profile(),
            audit=_audit(install),
            tools={"delete": FakeTool(lambda a: {})},
        )


@pytest.mark.parametrize("args", ["text", [1], 3])
def test_an_offered_tool_refuses_arguments_that_are_not_an_object(install, args):
    seen = []
    h = _host(install, lambda a: seen.append(a) or {})
    assert json.loads(h.call("search", args)["content"][0]["text"])["rule"] == ("arguments_invalid")
    assert seen == []


def test_missing_arguments_are_an_empty_object(install):
    seen = []
    _host(install, lambda a: seen.append(a) or {}).call("search", None)
    assert seen == [{}]


def test_a_guard_refusal_inside_a_tool_is_returned_and_audited(install):
    def run(args):
        raise GuardRefused(
            "cap_reached", "move_per_day", "the 24-hour cap is reached", action="move"
        )

    payload = json.loads(_host(install, run).call("search", {})["content"][0]["text"])
    assert (payload["error_code"], payload["exit_code"]) == ("cap_reached", 7)
    rec = _records(install)[-1]
    assert (rec["result"], rec["rule"], rec["action"]) == ("refused", "move_per_day", "move")


def test_a_crash_inside_a_tool_returns_no_detail(install):
    def run(args):
        raise RuntimeError("Geheim-Projekt")

    result = _host(install, run).call("search", {})
    assert result["isError"] is True and "Geheim" not in json.dumps(result)
    rec = _records(install)[-1]
    assert (rec["result"], rec["error_code"]) == ("error", "operation_error")
    assert "Geheim" not in install.read_text(encoding="utf-8")


def test_no_data_leaves_the_host_when_its_audit_line_cannot_be_written(install):
    """Fail closed: a read whose record cannot be written returns nothing."""
    h = _host(install, lambda args: {"secret": "Geheim-Projekt"})
    install.unlink()
    result = h.call("search", {})
    assert result["isError"] is True and "Geheim" not in json.dumps(result)
    assert json.loads(result["content"][0]["text"])["rule"] == "audit_log_missing"


# --- the process: stdout carries protocol frames only ---------------------------------


@pytest.mark.skipif(profile._bot_group() is not None, reason="a bot group is installed here")
def test_the_real_entry_point_refuses_on_a_machine_without_an_install():
    """No seam at all: the shipped module, this machine's real anchor path and groups."""
    r = subprocess.run(
        [sys.executable, "-m", "mailbox_cleanup.host"],
        input=_rpc(INIT),
        capture_output=True,
        timeout=30,
    )
    assert r.returncode == 6 and r.stdout == b""
    assert b"bot_group_missing" in r.stderr


DRIVER = textwrap.dedent("""
    import json, os, sys
    from pathlib import Path
    from mailbox_cleanup import guards, host, profile
    adir = Path(sys.argv[1])
    profile.ANCHOR_DIR = adir
    profile.ANCHOR_OWNER_UID = os.getuid()
    me = __import__("pwd").getpwuid(os.getuid()).pw_name
    profile._bot_group = lambda: (4242424, frozenset({me}))
    guards._is_append_only = lambda st: True

    class Loud:
        spec = {"name": "search", "description": "d", "inputSchema": {"type": "object"}}
        def run(self, args):
            print("STRAY-PRINT")
            os.write(1, b"STRAY-FD1\\n")
            return {"ok": 1}

    host.build_tools = lambda p: {"search": Loud()}
    sys.exit(host.main())
""")


def test_a_stray_print_never_reaches_the_protocol_stream(install, tmp_path):
    driver = tmp_path / "driver.py"
    driver.write_text(DRIVER, encoding="utf-8")
    r = subprocess.run(
        [sys.executable, str(driver), str(profile.ANCHOR_DIR)],
        input=_rpc(INIT, _call("search", {})),
        capture_output=True,
        timeout=30,
    )
    assert r.returncode == 0, r.stderr
    frames = [json.loads(x) for x in r.stdout.splitlines()]
    assert [f["id"] for f in frames] == [1, 2]
    assert b"STRAY-PRINT" in r.stderr and b"STRAY-FD1" in r.stderr
    assert b"STRAY" not in r.stdout
