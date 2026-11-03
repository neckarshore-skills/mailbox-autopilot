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
    for name in ("gate.py", "retention.py", "settings.json", "host.json", "launch.sh"):
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
    assert retention.prune_audit(log, 90, now, flags=False) == 1
    lines = log.read_text().splitlines()
    assert len(lines) == 3 and json.loads(lines[1])["n"] == 2
    assert stat.S_IMODE(log.stat().st_mode) == 0o640
    assert not (tmp_path / "host-audit.log.retention").exists()


def test_prune_audit_leaves_the_file_alone_when_nothing_is_old(tmp_path):
    now = time.time()
    log = tmp_path / "host-audit.log"
    log.write_text(_line(1, now))
    inode = log.stat().st_ino
    assert retention.prune_audit(log, 90, now, flags=False) == 0
    assert log.stat().st_ino == inode


def test_retention_imports_nothing_from_the_package():
    source = (LAUNCHER / "retention.py").read_text()
    assert not re.search(r"^\s*(from|import)\s+mailbox_cleanup", source, re.M)
