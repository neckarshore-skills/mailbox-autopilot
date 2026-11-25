"""#76 launch specification, slice 4b: what provisions the launcher on the target Mac.

Held here: the plists (L3, L9, L13), host.json against the package and the gate, every
pin against its twin (L4, L10), the pf anchor's words (L6), retention by age (L5), and
that install.sh refuses anywhere but the target Mac as root. Running install.sh, loading
the daemons, pf and the system append-only flag are measured on the target Mac only."""

import importlib.util
import json
import os
import re
import stat
import subprocess
import time
import tomllib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
LAUNCHER = ROOT / "launcher"


def _load(name):
    spec = importlib.util.spec_from_file_location(f"launcher_{name}", LAUNCHER / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


gate = _load("gate")
plists = _load("plists")
retention = _load("retention")


def _pin(path: Path, name: str) -> str:
    return re.search(rf'^{name}="([^"]+)"', path.read_text(), re.M).group(1)


# ---- plists ----


def test_the_launcher_runs_hourly_from_7_to_20_as_root_with_stderr():
    p = plists.launcher()
    assert [(e["Hour"], e["Minute"]) for e in p["StartCalendarInterval"]] == [
        (h, 0) for h in range(7, 21)
    ]
    assert "UserName" not in p and "GroupName" not in p
    assert p["StandardErrorPath"].startswith(gate.BASE.as_posix() + "/")
    assert p["ProgramArguments"] == ["/bin/bash", f"{gate.BASE}/launcher/launch.sh"]
    assert p["RunAtLoad"] is False and p["ExitTimeOut"] > 0


def test_retention_runs_daily_outside_the_run_hours():
    p = plists.retention()
    assert "UserName" not in p
    assert not 7 <= p["StartCalendarInterval"]["Hour"] <= 20
    args = p["ProgramArguments"]
    assert args[:2] == ["/usr/bin/env", "-i"] and "--offline" in args
    assert args[-1] == f"{retention.BASE}/launcher/retention.py"


def test_the_plists_print_as_valid_plist_and_refuse_anything_else(capsysbinary):
    import plistlib

    assert plists.main(["plists.py", "launcher"]) == 0
    assert plistlib.loads(capsysbinary.readouterr().out)["Label"] == plists.LABEL
    assert plists.main(["plists.py", "other"]) == 2


# ---- host.json ----


def test_host_json_starts_the_package_host_under_the_gates_server_name():
    servers = json.loads((LAUNCHER / "host.json").read_text())["mcpServers"]
    assert list(servers) == [gate.SERVER]
    command = Path(servers[gate.SERVER]["command"])
    scripts = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["scripts"]
    assert command.name in scripts and scripts[command.name] == "mailbox_cleanup.host:main"
    assert command.parent == Path("/usr/local/mailbox-autopilot/app/.venv/bin")
    assert servers[gate.SERVER]["args"] == [] and "env" not in servers[gate.SERVER]


# ---- every pin against its twin ----


def test_the_uv_pins_agree_everywhere():
    ci = set(re.findall(r'version:\s*"([0-9.]+)"', (ROOT / ".github/workflows/ci.yml").read_text()))
    install = LAUNCHER / "install.sh"
    probe = ROOT / "scripts/keychain_probe/probe.sh"
    assert ci == {_pin(install, "UV_VERSION")} == {_pin(LAUNCHER / "launch.sh", "UV_VERSION")}
    assert _pin(install, "UV_VERSION") == _pin(probe, "UV_VERSION")
    assert _pin(install, "UV_SHA256") == _pin(probe, "UV_SHA256")


def test_the_claude_pins_agree():
    install = LAUNCHER / "install.sh"
    assert _pin(install, "CLAUDE_VERSION") == gate.CLAUDE_VERSION
    assert re.fullmatch(r"[0-9a-f]{64}", _pin(install, "CLAUDE_SHA256"))
    assert gate.CLAUDE.as_posix() == "/usr/local/mailbox-autopilot/claude/claude"


def test_the_installed_paths_are_the_launchers_paths():
    text = (LAUNCHER / "install.sh").read_text()
    assert (
        _pin(LAUNCHER / "install.sh", "BASE") == gate.BASE.as_posix() == retention.BASE.as_posix()
    )
    assert _pin(LAUNCHER / "launch.sh", "UV") == plists.UV
    assert _pin(LAUNCHER / "install.sh", "BOT_USER") == gate.BOT_USER
    for name in (
        "digest.py",
        "gate.py",
        "host.json",
        "launch.sh",
        "prompt.txt",
        "retention.py",
        "settings.json",
    ):
        assert name in text


# ---- install.sh ----


def test_install_refuses_anywhere_but_the_target_mac_as_root():
    proc = subprocess.run(
        ["bash", str(LAUNCHER / "install.sh"), "status"], capture_output=True, text=True
    )
    assert proc.returncode == 2
    assert re.search(r"target Mac only|runs on M1mini only|run it with sudo", proc.stderr)


def test_install_neither_loads_daemons_nor_edits_pf_conf():
    text = (LAUNCHER / "install.sh").read_text()
    code = "\n".join(x for x in text.splitlines() if not x.lstrip().startswith("#"))
    assert "launchctl bootstrap" not in code and "launchctl load" not in code
    assert "pfctl" not in code and not re.search(r">>?\s*/etc/pf\.conf", code)


# ---- pf ----


def test_the_pf_anchor_allows_443_and_993_out_and_blocks_the_rest_for_the_bot():
    rules = [
        x
        for x in (LAUNCHER / "pf.anchor").read_text().splitlines()
        if x.strip() and not x.startswith("#")
    ]
    assert rules == [
        "pass out quick proto tcp from any to any port { 443, 993 } "
        "user mailbox-autopilot-bot keep state",
        "block drop out quick user mailbox-autopilot-bot",
        "block drop in quick user mailbox-autopilot-bot",
    ]


# ---- retention (L5) ----


def _aged(path: Path, days: float, now: float) -> Path:
    path.write_text("x")
    os.utime(path, (now - days * 86400, now - days * 86400))
    return path


@pytest.mark.parametrize(("limit", "old", "young"), [(14, 14.1, 13.9), (30, 30.1, 29.9)])
def test_prune_dir_removes_files_past_the_limit_and_keeps_younger(tmp_path, limit, old, young):
    now = time.time()
    _aged(tmp_path / "old", old, now)
    _aged(tmp_path / "young", young, now)
    (tmp_path / "link").symlink_to(tmp_path / "young")
    os.utime(tmp_path / "link", (0, 0), follow_symlinks=False)
    assert retention.prune_dir(tmp_path, limit, now) == ["old"]
    assert sorted(p.name for p in tmp_path.iterdir()) == ["link", "young"]


def test_the_limits_are_14_30_and_90_days():
    assert (retention.RUN_DAYS, retention.DIGEST_DAYS, retention.AUDIT_DAYS) == (14, 30, 90)
    assert retention.RUNS_DIR == gate.RUNS_DIR


def _line(days_ago: float, now: float, **extra) -> str:
    stamp = (datetime.fromtimestamp(now, UTC) - timedelta(days=days_ago)).isoformat()
    return json.dumps({"timestamp": stamp.replace("+00:00", "Z"), **extra}) + "\n"


def test_prune_audit_drops_lines_past_90_days_and_keeps_unreadable_ones(tmp_path):
    now = time.time()
    log = tmp_path / "host-audit.log"
    log.write_text(_line(91, now, n=1) + "not json\n" + _line(89, now, n=2) + '{"no": "time"}\n')
    log.chmod(0o640)
    assert retention.prune_audit(log, 90, now, flag=None) == 1
    lines = log.read_text().splitlines()
    assert len(lines) == 3 and json.loads(lines[1])["n"] == 2
    assert stat.S_IMODE(log.stat().st_mode) == 0o640
    assert not (tmp_path / "host-audit.log.retention").exists()


def test_prune_audit_leaves_the_file_alone_when_nothing_is_old(tmp_path):
    now = time.time()
    log = tmp_path / "host-audit.log"
    log.write_text(_line(1, now))
    inode = log.stat().st_ino
    assert retention.prune_audit(log, 90, now, flag=None) == 0
    assert log.stat().st_ino == inode


def test_retention_imports_nothing_from_the_package():
    source = (LAUNCHER / "retention.py").read_text()
    assert not re.search(r"^\s*(from|import)\s+mailbox_cleanup", source, re.M)


darwin = pytest.mark.skipif(not hasattr(os, "chflags"), reason="chflags is BSD and macOS only")


@pytest.fixture
def flagged_log(tmp_path):
    log = tmp_path / "host-audit.log"
    yield log
    for p in tmp_path.iterdir():
        os.chflags(p, 0)


@darwin
def test_prune_audit_runs_the_real_flag_sequence_with_the_user_flag(flagged_log):
    now = time.time()
    flagged_log.write_text(_line(91, now, n=1) + _line(1, now, n=2))
    os.chflags(flagged_log, stat.UF_APPEND)
    assert retention.prune_audit(flagged_log, 90, now, flag=stat.UF_APPEND) == 1
    assert [json.loads(x)["n"] for x in flagged_log.read_text().splitlines()] == [2]
    assert flagged_log.stat().st_flags & stat.UF_APPEND
    assert sorted(p.name for p in flagged_log.parent.iterdir()) == ["host-audit.log"]


@darwin
def test_a_run_that_died_after_the_clear_is_healed_by_the_next(flagged_log):
    now = time.time()
    flagged_log.write_text(_line(1, now))
    (flagged_log.parent / "host-audit.log.retention").write_text("half written")
    assert retention.prune_audit(flagged_log, 90, now, flag=stat.UF_APPEND) == 0
    assert flagged_log.stat().st_flags & stat.UF_APPEND
    assert sorted(p.name for p in flagged_log.parent.iterdir()) == ["host-audit.log"]


# ---- stderr rotation (L13 files, L5 run-file class: 14 days) ----


def test_the_rotated_files_are_the_plists_stderr_paths():
    assert sorted(retention.STDERR_FILES) == sorted(
        [
            Path(plists.launcher()["StandardErrorPath"]),
            Path(plists.retention()["StandardErrorPath"]),
        ]
    )


def test_rotate_moves_a_filled_stderr_file_and_leaves_an_empty_one(tmp_path):
    full, empty = tmp_path / "launcher.stderr", tmp_path / "retention.stderr"
    full.write_text("uv refused\n")
    empty.write_text("")
    dest = tmp_path / "stderr"
    moved = retention.rotate_stderr([full, empty, tmp_path / "absent.stderr"], dest, 1764000000.0)
    assert moved == ["launcher.stderr-20251124T160000Z"]
    assert empty.exists()
    assert (dest / moved[0]).read_text() == "uv refused\n"
    assert stat.S_IMODE(dest.stat().st_mode) == 0o700


def test_rotate_leaves_an_empty_owner_only_file_where_launchd_appends(tmp_path):
    """C6: launchd creates a missing StandardErrorPath file 0644 whatever the Umask key
    says, and appends to an existing one without touching its mode (both measured
    2026-11-25 on macOS 27.2.1, gui domain, with and without Umask 63). So after a move,
    and for a file that is absent, rotation leaves an empty 0600 file in the path."""
    full, absent = tmp_path / "launcher.stderr", tmp_path / "retention.stderr"
    full.write_text("x\n")
    retention.rotate_stderr([full, absent], tmp_path / "stderr", time.time())
    for path in (full, absent):
        assert path.read_text() == ""
        assert stat.S_IMODE(path.lstat().st_mode) == 0o600


def test_install_creates_the_stderr_files_owner_only_before_launchd_does():
    text = (LAUNCHER / "install.sh").read_text()
    names = sorted(p.name for p in retention.STDERR_FILES)
    assert f"for f in {' '.join(names)}; do" in text
    assert 'install -o root -g wheel -m 600 /dev/null "$BASE/$f"' in text
    assert 'chmod 600 "$BASE/$f"' in text


def test_rotate_leaves_a_symlink_alone(tmp_path):
    target = tmp_path / "target"
    target.write_text("x")
    link = tmp_path / "launcher.stderr"
    link.symlink_to(target)
    assert retention.rotate_stderr([link], tmp_path / "stderr", time.time()) == []
    assert link.is_symlink()


def test_rotated_stderr_is_pruned_at_14_days(tmp_path):
    dest = tmp_path / "stderr"
    dest.mkdir()
    old, new = dest / "launcher.stderr-old", dest / "launcher.stderr-new"
    old.write_text("x")
    new.write_text("y")
    now = time.time()
    os.utime(old, (now - 15 * 86400, now - 15 * 86400))
    assert retention.prune_dir(dest, retention.RUN_DAYS, now) == ["launcher.stderr-old"]


# ---- C3 (James, #101): the night gap is 11 hours, so retention kills leftovers too ----


def test_retention_kills_the_bot_users_leftovers_first(tmp_path, monkeypatch, capsys):
    seen = []
    for name in ("RUNS_DIR", "DIGESTS_DIR", "HOST_AUDIT", "STDERR_DIR"):
        monkeypatch.setattr(retention, name, tmp_path / name)
    monkeypatch.setattr(retention, "STDERR_FILES", ())
    monkeypatch.setattr(
        retention.subprocess,
        "run",
        lambda argv, **k: seen.append(argv) or subprocess.CompletedProcess(argv, 0),
    )
    assert retention.main() == 0
    assert seen == [["/usr/bin/pkill", "-KILL", "-U", "mailbox-autopilot-bot"]]
    report = json.loads(capsys.readouterr().err)["retention"]
    assert report["leftovers_killed"] is True


def test_retention_and_the_gate_name_the_same_bot_user():
    assert retention.BOT_USER == gate.BOT_USER


# ---- C6 (James, #102): launchd creates the stderr files, so the umask is set there ----


@pytest.mark.parametrize("which", ["launcher", "retention"])
def test_both_daemons_run_with_an_owner_only_umask(which):
    """Umask 0o077 (decimal 63, as plist integers are written), for the files a job
    creates. Measured 2026-11-25 (macOS 27.2.1, gui domain): a file the job touched came
    out 0600 with the key and 0644 without; the StandardErrorPath file launchd created
    came out 0644 either way, which the two tests below cover. System domain: 5b run."""
    p = getattr(plists, which)()
    assert p["Umask"] == 0o077 == 63


# ---- C7 (James, #102; CodeRabbit 4507077146, 4507077153): the audit log is a control ----


def test_a_short_write_does_not_drop_audit_records(tmp_path, monkeypatch):
    now = time.time()
    log = tmp_path / "host-audit.log"
    kept = "".join(_line(1, now, n=i) for i in range(20))
    log.write_text(_line(91, now, n=-1) + kept)
    real_write = os.write
    monkeypatch.setattr(retention.os, "write", lambda fd, data: real_write(fd, data[:7]))
    assert retention.prune_audit(log, 90, now, flag=None) == 1
    assert log.read_text() == kept


@darwin
def test_a_failed_replace_puts_the_flag_back_and_says_so(flagged_log, monkeypatch):
    now = time.time()
    flagged_log.write_text(_line(91, now, n=1) + _line(1, now, n=2))
    os.chflags(flagged_log, stat.UF_APPEND)

    def refuse(src, dst):
        raise OSError("replace refused")

    monkeypatch.setattr(retention.os, "replace", refuse)
    with pytest.raises(OSError, match="replace refused"):
        retention.prune_audit(flagged_log, 90, now, flag=stat.UF_APPEND)
    assert flagged_log.stat().st_flags & stat.UF_APPEND
    assert len(flagged_log.read_text().splitlines()) == 2  # nothing lost


# ---- CodeRabbit 4507077140 on #102: a timestamp without a time zone ----


def test_a_timestamp_without_a_zone_is_kept_and_retention_goes_on(tmp_path):
    """A naive time compared with the aware cutoff raised TypeError outside the parse
    guard and stopped the whole retention run. A line whose time has no zone is kept,
    like any line whose time cannot be read: losing a record is worse."""
    now = time.time()
    log = tmp_path / "host-audit.log"
    naive = json.dumps({"timestamp": "2020-01-01T00:00:00", "n": 0}) + "\n"
    log.write_text(naive + _line(91, now, n=1) + _line(1, now, n=2))
    assert retention.prune_audit(log, 90, now, flag=None) == 1
    assert [json.loads(x)["n"] for x in log.read_text().splitlines()] == [0, 2]
