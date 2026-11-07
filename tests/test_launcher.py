"""#76 launch specification, slice 1: the launcher core (launcher/gate.py, launcher/launch.sh).

What can be held anywhere is held here: the init gate (L8), the empty environment (L1),
the literal argv (L1, L2, L12), the wall clock (L12), the uv check (L10), the run file
(L5) and the refusing credential seam (L11). Root ownership, the privilege drop, the plist
and 5a to 5c are measured on the target Mac only, and nothing here stands in for them."""

import importlib.util
import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from mailbox_cleanup import host

ROOT = Path(__file__).resolve().parent.parent
LAUNCHER = ROOT / "launcher"


def _load_gate():
    spec = importlib.util.spec_from_file_location("launcher_gate", LAUNCHER / "gate.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


gate = _load_gate()

# Captured on the laptop, Claude Code 2.1.317, from build_argv() with the launcher's
# environment and a throwaway stdio server named `mailbox` offering search and read.
# Session id, paths and capability flags removed; the fields the gate reads are verbatim.
GOOD_INIT = {
    "type": "system",
    "subtype": "init",
    "tools": ["mcp__mailbox__read", "mcp__mailbox__search"],
    "mcp_servers": [{"name": "mailbox", "status": "connected", "source": "dynamic"}],
    "agents": ["claude", "Explore", "general-purpose", "Plan", "statusline-setup"],
    "skills": [],
    "slash_commands": [],
    "permissionMode": "default",
    "apiKeySource": "none",
    "analytics_disabled": True,
    "claude_code_version": "2.1.317",
}


def _fake_claude(tmp_path: Path, events: list, after: str = "") -> Path:
    """A stand-in for claude: prints the given events as stream-json lines, then runs
    `after`. A child that survives the gate writes `survived` next to itself."""
    script = tmp_path / "claude"
    lines = "\n".join(json.dumps(e) if not isinstance(e, str) else e for e in events)
    script.write_text(
        f"#!{sys.executable}\n"
        "import sys, time, pathlib\n"
        "sys.stdin.read()\n"
        f"sys.stdout.write({lines!r} + '\\n' if {lines!r} else '')\n"
        "sys.stdout.flush()\n"
        f"{after}\n"
    )
    script.chmod(0o755)
    return script


SURVIVE = "time.sleep(2.0); pathlib.Path(__file__).with_name('survived').write_text('x')"
# A grandchild in the same process group, standing in for the MCP host, that outlives claude.
ORPHAN = (
    "import subprocess; subprocess.Popen([sys.executable, '-c', "
    "'import time, pathlib, sys; time.sleep(2.0); pathlib.Path(sys.argv[1]).write_text(\"x\")', "
    "str(pathlib.Path(__file__).with_name('survived'))], stdout=subprocess.DEVNULL)"
)


def _run(tmp_path, events, after="", out=None, **kw):
    claude = _fake_claude(tmp_path, events, after)
    out_path = tmp_path / "run.jsonl"
    with out_path.open("w") as real:
        rc = gate.supervise(
            [str(claude)],
            {"PATH": "/usr/bin:/bin"},
            "prompt",
            out or real,
            drop=False,
            cwd=tmp_path,
            **kw,
        )
    lines = [json.loads(x) for x in out_path.read_text().splitlines()]
    # Nothing waits for a killed child, so a survivor shows only once its sleep is over.
    survived = _survived_later(tmp_path) if "survived" in after else False
    return rc, lines, survived


def _survived_later(tmp_path) -> bool:
    import time

    time.sleep(2.5)
    return (tmp_path / "survived").exists()


# ---- L8, the init gate ----


def test_good_init_runs_and_every_line_reaches_the_run_file(tmp_path):
    rc, lines, _ = _run(tmp_path, [GOOD_INIT, {"type": "result", "subtype": "success"}])
    assert rc == gate.EXIT_OK
    assert lines == [GOOD_INIT, {"type": "result", "subtype": "success"}]


@pytest.mark.parametrize(
    "change",
    [
        {"tools": GOOD_INIT["tools"] + ["Bash"]},
        {"tools": ["mcp__mailbox__search"]},
        {"tools": GOOD_INIT["tools"] + ["mcp__mailbox__move"]},
        {"tools": GOOD_INIT["tools"] + ["mcp__mailbox__read"]},
        {"tools": ["mcp__other__search", "mcp__other__read"]},
        {"mcp_servers": [{"name": "mailbox"}, {"name": "second"}]},
        {"mcp_servers": []},
        {"tools": None},
        {"subtype": "status"},
    ],
    ids=[
        "bash",
        "missing",
        "later-stage",
        "duplicate",
        "other-server",
        "two-servers",
        "no-server",
        "no-tools",
        "not-init",
    ],
)
def test_a_mismatched_init_kills_the_child_before_it_can_go_on(tmp_path, change):
    rc, lines, survived = _run(
        tmp_path, [{**GOOD_INIT, **change}, {"type": "assistant"}], after=SURVIVE
    )
    assert rc == gate.EXIT_GATE
    assert lines[-1]["launcher"]["refused"] == "gate"
    assert {"type": "assistant"} not in lines
    assert not survived


def test_a_first_line_that_is_not_json_is_refused(tmp_path):
    rc, lines, survived = _run(tmp_path, ["hello"], after=SURVIVE)
    assert rc == gate.EXIT_GATE
    assert not survived


def test_a_silent_child_is_killed_at_the_init_timeout(tmp_path):
    rc, lines, survived = _run(tmp_path, [], after=SURVIVE, init_timeout_s=0.3)
    assert rc == gate.EXIT_NO_INIT
    assert lines[-1]["launcher"]["refused"] == "no_init"
    assert not survived


def test_a_stream_that_ends_without_init_is_refused(tmp_path):
    rc, lines, _ = _run(tmp_path, [])
    assert rc == gate.EXIT_NO_INIT


# ---- L12, the wall clock ----


def test_the_wall_clock_kills_a_session_that_passed_the_gate(tmp_path):
    rc, lines, survived = _run(tmp_path, [GOOD_INIT], after=SURVIVE, wall_clock_s=1.5)
    assert rc == gate.EXIT_WALL_CLOCK
    assert lines[0] == GOOD_INIT
    assert lines[-1]["launcher"]["refused"] == "wall_clock"
    assert not survived


def test_the_wall_clock_holds_after_the_child_closes_its_stdout(tmp_path):
    after = "import os; os.close(1); " + SURVIVE
    rc, lines, survived = _run(tmp_path, [GOOD_INIT], after=after, wall_clock_s=1.5)
    assert rc == gate.EXIT_WALL_CLOCK
    assert not survived


def test_a_child_that_fails_is_reported(tmp_path):
    rc, lines, _ = _run(tmp_path, [GOOD_INIT], after="sys.exit(3)")
    assert rc == gate.EXIT_CHILD
    assert lines[-1] == {"launcher": {"child_exit": 3}}


def test_a_last_line_without_newline_still_reaches_the_run_file(tmp_path):
    after = 'sys.stdout.write(\'{"type": "result"}\'); sys.stdout.flush()'
    rc, lines, _ = _run(tmp_path, [GOOD_INIT], after=after)
    assert rc == gate.EXIT_OK
    assert lines[-1] == {"type": "result"}


# ---- fail closed: whatever ends the launcher ends the session ----


def test_a_host_that_outlives_a_clean_session_is_killed(tmp_path):
    rc, _, _ = _run(tmp_path, [GOOD_INIT], after=ORPHAN)
    assert rc == gate.EXIT_OK
    assert not _survived_later(tmp_path)


class _BrokenRunFile:
    """A run file that takes the init line and then fails, like a full disk."""

    def __init__(self):
        self.writes = 0

    def write(self, text):
        self.writes += 1
        if self.writes > 1:
            raise OSError(28, "No space left on device")

    def flush(self):
        pass


def test_an_error_in_the_launcher_kills_the_session(tmp_path):
    with pytest.raises(OSError):
        _run(tmp_path, [GOOD_INIT, {"type": "assistant"}], after=ORPHAN + "; " + SURVIVE,
             out=_BrokenRunFile())  # fmt: skip
    assert not _survived_later(tmp_path)


def test_sigterm_to_the_launcher_kills_the_session(tmp_path):
    import signal
    import threading

    timer = threading.Timer(0.5, os.kill, (os.getpid(), signal.SIGTERM))
    timer.start()
    try:
        with pytest.raises(gate.Terminated):
            _run(tmp_path, [GOOD_INIT], after=ORPHAN + "; " + SURVIVE)
    finally:
        timer.cancel()
    assert signal.getsignal(signal.SIGTERM) is not gate._terminated
    assert not _survived_later(tmp_path)


# ---- L1, L2, environment and argv ----


def test_the_environment_is_built_from_nothing(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://elsewhere.example")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "planted")
    env = gate.build_env()
    assert set(env) == {
        "PATH",
        "HOME",
        "LANG",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC",
        "CLAUDE_CODE_DISABLE_AUTO_MEMORY",
        "DISABLE_UPDATES",
    }
    assert not any(k.startswith("ANTHROPIC") for k in env)


def test_the_invocation_is_literal():
    assert gate.build_argv(Path("/c"), Path("/h.json"), Path("/s.json")) == [
        "/c", "-p",
        "--tools", "",
        "--allowedTools", "mcp__mailbox__read", "mcp__mailbox__search",
        "--strict-mcp-config", "--mcp-config", "/h.json",
        "--disable-slash-commands",
        "--setting-sources", "",
        "--settings", "/s.json",
        "--no-session-persistence",
        "--output-format", "stream-json", "--verbose",
        "--max-turns", str(gate.MAX_TURNS),
    ]  # fmt: skip


def test_no_bypass_and_no_bare_can_slip_in():
    argv = gate.build_argv()
    for word in (
        "--bare",
        "--dangerously-skip-permissions",
        "bypassPermissions",
        "--plugin-dir",
        "--permission-mode",
    ):
        assert word not in argv
    assert "*" not in "".join(argv)


# ---- the two keys: literals here, held against the host and CI ----


def test_the_pinned_tools_are_the_hosts_release_1_tools():
    assert {
        t.removeprefix(f"mcp__{gate.SERVER}__") for t in gate.PINNED_TOOLS
    } == host.ALLOWED_TOOLS


def test_the_launcher_imports_nothing_from_the_package():
    source = (LAUNCHER / "gate.py").read_text()
    assert not re.search(r"^\s*(from|import)\s+mailbox_cleanup", source, re.M)


def test_the_uv_pin_is_the_ci_pin():
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
    pins = set(re.findall(r'version:\s*"([0-9.]+)"', ci))
    launch = (LAUNCHER / "launch.sh").read_text()
    assert pins == {re.search(r'^UV_VERSION="([0-9.]+)"', launch, re.M).group(1)}


# ---- L11 seam and L5 run file ----


def _main_without_root(tmp_path, monkeypatch, version_reason=None):
    calls = []
    monkeypatch.setattr(gate, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(gate, "kill_leftovers", lambda: calls.append("kill") or 0)
    monkeypatch.setattr(
        gate, "check_claude_version", lambda *a, **k: calls.append("version") or version_reason
    )
    monkeypatch.setattr(gate, "supervise", lambda *a, **k: pytest.fail("a session started"))
    rc = gate.main()
    (run,) = (tmp_path / "runs").iterdir()
    return rc, [json.loads(x)["launcher"] for x in run.read_text().splitlines()], calls


def test_the_credential_seam_refuses_so_no_session_starts(tmp_path, monkeypatch):
    rc, notes, calls = _main_without_root(tmp_path, monkeypatch)
    assert rc == gate.EXIT_CREDENTIAL
    assert calls == ["kill", "version"]
    assert notes[-1]["refused"] == "credential"


def test_a_wrong_claude_version_stops_before_the_credential_step(tmp_path, monkeypatch):
    monkeypatch.setattr(gate, "credential_step", lambda env: pytest.fail("credential read"))
    rc, notes, _ = _main_without_root(tmp_path, monkeypatch, "claude version: expected x")
    assert rc == gate.EXIT_VERSION
    assert notes[-1] == {"refused": "version", "reason": "claude version: expected x"}


def _version_script(tmp_path, says: str, code: int = 0) -> Path:
    script = tmp_path / "claude-version"
    script.write_text(f"#!/bin/sh\necho '{says}'\nexit {code}\n")
    script.chmod(0o755)
    return script


def test_the_pinned_claude_version_passes(tmp_path):
    claude = _version_script(tmp_path, f"{gate.CLAUDE_VERSION} (Claude Code)")
    assert gate.check_claude_version(claude, {"PATH": "/usr/bin:/bin"}, drop=False) is None


@pytest.mark.parametrize(
    ("says", "code"),
    [("2.1.318 (Claude Code)", 0), ("", 0), ("2.1.317 (Claude Code)", 1)],
    ids=["other", "empty", "failed"],
)
def test_another_claude_version_is_refused(tmp_path, says, code):
    claude = _version_script(tmp_path, says, code)
    reason = gate.check_claude_version(claude, {"PATH": "/usr/bin:/bin"}, drop=False)
    assert reason is not None and gate.CLAUDE_VERSION in reason


def test_a_missing_claude_is_refused(tmp_path):
    reason = gate.check_claude_version(tmp_path / "nope", {}, drop=False)
    assert reason == "claude --version failed: FileNotFoundError"


def test_leftovers_are_killed_by_the_bot_users_uid(monkeypatch):
    seen = []
    monkeypatch.setattr(gate.subprocess, "run", lambda argv, **k: seen.append(argv) or
                        subprocess.CompletedProcess(argv, 1))  # fmt: skip
    gate.kill_leftovers()
    assert seen == [["/usr/bin/pkill", "-KILL", "-U", "mailbox-autopilot-bot"]]


def test_the_run_file_is_owner_only(tmp_path):
    with gate.open_run_file(tmp_path / "runs") as out:
        out.write("x\n")
    (run,) = (tmp_path / "runs").iterdir()
    assert stat.S_IMODE(run.stat().st_mode) == 0o600
    assert stat.S_IMODE((tmp_path / "runs").stat().st_mode) == 0o700


# ---- L10, launch.sh ----


def _launch(tmp_path, uv_says: str | None, planted_env=None):
    """launch.sh with its pinned paths pointed into tmp_path. The fake uv answers
    --version with `uv_says`, and on `run` prints its argv and environment as JSON."""
    uv = tmp_path / "uv"
    if uv_says is not None:
        uv.write_text(
            "#!/bin/bash\n"
            f'if [ "$1" = "--version" ]; then echo "{uv_says}"; exit 0; fi\n'
            f"exec {sys.executable} -c 'import json,os,sys; "
            f'print(json.dumps({{"argv": sys.argv[1:], "env": dict(os.environ)}}))\' "$@"\n'
        )
        uv.chmod(0o755)
    runs = tmp_path / "runs"
    runs.mkdir()
    script = (LAUNCHER / "launch.sh").read_text()
    script = re.sub(r'^UV=".*"$', f'UV="{uv}"', script, flags=re.M)
    script = re.sub(r'^RUNS_DIR=".*"$', f'RUNS_DIR="{runs}"', script, flags=re.M)
    copy = tmp_path / "launch.sh"
    copy.write_text(script)
    env = {"PATH": os.environ["PATH"], **(planted_env or {})}
    proc = subprocess.run(["bash", str(copy)], capture_output=True, text=True, env=env)
    return proc, runs


def test_launch_refuses_a_uv_of_another_version_with_one_line(tmp_path):
    proc, runs = _launch(tmp_path, "uv 0.13.6 (Homebrew 2026-10-20)")
    assert proc.returncode == 20
    assert "expected 0.13.5, found 0.13.6" in proc.stderr
    (run,) = runs.iterdir()
    assert len(run.read_text().splitlines()) == 1
    assert stat.S_IMODE(run.stat().st_mode) == 0o600


def test_launch_refuses_when_uv_is_missing(tmp_path):
    proc, _ = _launch(tmp_path, None)
    assert proc.returncode == 20
    assert "no uv at" in proc.stderr


def test_launch_starts_the_gate_from_an_empty_environment(tmp_path):
    proc, _ = _launch(
        tmp_path,
        "uv 0.13.5",
        {
            "ANTHROPIC_BASE_URL": "https://elsewhere.example",
            "UV_INDEX_URL": "https://elsewhere.example",
        },
    )
    assert proc.returncode == 0, proc.stderr
    seen = json.loads(proc.stdout)
    assert seen["argv"][:2] == ["run", "--no-config"]
    assert "--offline" in seen["argv"] and seen["argv"][-1].endswith("/launcher/gate.py")
    assert not any(k.startswith(("ANTHROPIC", "UV_INDEX")) for k in seen["env"])
    assert seen["env"]["UV_PYTHON_DOWNLOADS"] == "never"


def test_the_bot_settings_turn_auto_memory_off_and_deny_built_in_tools():
    settings = json.loads((LAUNCHER / "settings.json").read_text())
    assert settings["autoMemoryEnabled"] is False
    assert {"Bash", "Edit", "Read", "WebFetch", "WebSearch", "Write"} <= set(
        settings["permissions"]["deny"]
    )
    assert "allow" not in settings["permissions"]
    assert "hooks" not in settings and "env" not in settings


def test_launch_never_follows_a_link_planted_in_the_owners_runs_directory(tmp_path):
    """The runs directory belongs to the owner, and launch.sh runs as root: a symlink
    under the refusal file's name must not make root write through it."""
    import time

    target = tmp_path / "elsewhere"
    target.write_text("untouched\n")
    runs = tmp_path / "runs"
    runs.mkdir()
    now = int(time.time())
    for t in range(now - 1, now + 5):
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(t))
        (runs / f"run-{stamp}-uv.jsonl").symlink_to(target)
    uv = tmp_path / "uv"
    uv.write_text('#!/bin/bash\necho "uv 0.0.1"\n')
    uv.chmod(0o755)
    script = (LAUNCHER / "launch.sh").read_text()
    script = re.sub(r'^UV=".*"$', f'UV="{uv}"', script, flags=re.M)
    script = re.sub(r'^RUNS_DIR=".*"$', f'RUNS_DIR="{runs}"', script, flags=re.M)
    copy = tmp_path / "launch.sh"
    copy.write_text(script)
    proc = subprocess.run(["bash", str(copy)], capture_output=True, text=True)
    assert proc.returncode == 20
    assert target.read_text() == "untouched\n"
