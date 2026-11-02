"""#76 L11: smoke tests for the keychain probe. The probe itself runs only on the target
Mac, with the owner at the keyboard, so these hold what can be held anywhere: it refuses
everywhere else, its secrets stay out of argv, and the value it reads is never printed."""

import importlib.util
import json
import os
import plistlib
import re
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import keyring.backends.macOS as macos
import keyring.errors
import pytest

ROOT = Path(__file__).resolve().parent.parent
PROBE = ROOT / "scripts" / "keychain_probe"


def _load(name):
    spec = importlib.util.spec_from_file_location(f"keychain_probe_{name}", PROBE / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


job = _load("job")
show = _load("show")


def test_the_runner_refuses_anywhere_but_the_target_mac_as_root():
    r = subprocess.run(
        ["bash", str(PROBE / "probe.sh"), "setup"], capture_output=True, text=True, timeout=30
    )
    assert r.returncode == 2
    assert "probe:" in r.stderr


def test_the_runner_can_be_started_as_the_sheet_says():
    """James on #97, finding 1: the sheet says `sudo ./probe.sh`."""
    assert os.access(PROBE / "probe.sh", os.X_OK)


def _script_commands() -> set[str]:
    script = (PROBE / "probe.sh").read_text(encoding="utf-8")
    return set(re.findall(r"^  ([a-z0-9-]+)\) cmd_", script, re.M))


def test_every_command_of_the_run_sheet_exists():
    sheet = (ROOT / "docs/2026-11-02-keychain-probe-runsheet.md").read_text(encoding="utf-8")
    used = set(re.findall(r"probe\.sh ([a-z0-9-]+)", sheet))
    assert {"setup", "arm3", "arm4b", "status", "cleanup"} <= used
    assert used <= _script_commands()


def test_the_output_directory_is_not_open_to_everyone():
    """James on #97, finding 5: root writes predictable names there."""
    script = (PROBE / "probe.sh").read_text(encoding="utf-8")
    assert "1777" not in script and "chown -h" in script


def test_setup_keeps_root_out_of_the_owners_home():
    """James on #97, finding 6: sudo keeps HOME on macOS, and uv writes there."""
    script = (PROBE / "probe.sh").read_text(encoding="utf-8")
    assert "export HOME=/var/root" in script


def test_the_runner_is_valid_bash():
    subprocess.run(["bash", "-n", str(PROBE / "probe.sh")], check=True, timeout=30)


def test_the_uv_it_installs_is_the_one_ci_pins():
    ci = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    pins = set(re.findall(r'version: "(\d+\.\d+\.\d+)"', ci))
    script = (PROBE / "probe.sh").read_text(encoding="utf-8")
    assert re.search(r'^UV_VERSION="(.+?)"', script, re.M).group(1) in pins


def test_the_probe_reads_with_the_locked_keyring():
    pin = re.search(r'"keyring==([\d.]+)"', (ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    requirements = (PROBE / "requirements.txt").read_text(encoding="utf-8")
    assert f"keyring=={pin.group(1)}" in requirements
    assert "--hash=sha256:" in requirements


def _argv():
    return ["--arm", "armX", "--mode", "username", "--action", "read"] + [
        "--user",
        "u",
        "--group",
        "g",
    ]


@pytest.fixture
def no_system(monkeypatch):
    monkeypatch.setattr(job, "facts", lambda group: {"uid": 1})


def test_the_value_never_reaches_the_output(no_system, monkeypatch, capsys):
    monkeypatch.setattr(macos.Keyring, "get_password", lambda self, s, a: job.CANARY)
    assert job.main(_argv()) == 0
    out = capsys.readouterr().out
    assert job.CANARY not in out
    assert json.loads(out)["read"] == {
        "outcome": "value",
        "osstatus": None,
        "value_equals_canary": True,
    }


def test_a_value_that_is_not_the_canary_is_not_taken_for_it(no_system, monkeypatch, capsys):
    monkeypatch.setattr(macos.Keyring, "get_password", lambda self, s, a: "something else")
    job.main(_argv())
    out = capsys.readouterr().out
    assert "something else" not in out and json.loads(out)["read"]["value_equals_canary"] is False


def _raises(kind, status):
    def get_password(self, service, account):
        try:
            raise OSError(status, "from the Security framework")
        except OSError as e:
            raise kind("cannot read") from e

    return get_password


@pytest.mark.parametrize(
    "answer, outcome, status",
    [
        (lambda self, s, a: None, "none", None),
        (_raises(keyring.errors.KeyringLocked, -128), "KeyringLocked", -128),
        (_raises(keyring.errors.KeyringError, -25308), "KeyringError", -25308),
        (_raises(RuntimeError, -1), "other", -1),
    ],
    ids=["missing", "locked", "not-allowed", "other"],
)
def test_each_answer_of_the_keychain_is_told_apart(monkeypatch, answer, outcome, status):
    """Arm 7 needs missing told from locked, or a red arm proves nothing."""
    monkeypatch.setattr(macos.Keyring, "get_password", answer)
    read = job.read_item(job.SERVICE)
    assert (read["outcome"], read["osstatus"]) == (outcome, status)


def test_a_failing_arm_still_prints_its_line(monkeypatch, capsys):
    def broken(group):
        raise OSError("no such thing")

    monkeypatch.setattr(job, "facts", broken)
    assert job.main(_argv()) == 0
    assert json.loads(capsys.readouterr().out)["failed"] == "OSError"


@pytest.fixture
def security(monkeypatch, tmp_path):
    calls = []

    def run(argv, input=None, **kw):
        calls.append((argv, input))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(job.subprocess, "run", run)
    monkeypatch.setenv("HOME", str(tmp_path))
    return calls


def _scripts(calls) -> str:
    return "\n".join(script for _, script in calls)


def test_the_unlock_secret_goes_to_security_on_stdin_never_in_argv(security):
    job.prepare("s3cret", trust=True, partition="unsigned")
    assert all(argv == [job.SECURITY, "-i"] for argv, _ in security)
    script = _scripts(security)
    assert "create-keychain -p s3cret" in script
    assert "-S apple-tool:,apple:,unsigned: " in script


def test_the_cdhash_half_names_this_interpreter(security, monkeypatch):
    """James on #97, section 2: an ad-hoc signed binary may get a `cdhash:` partition."""
    monkeypatch.setattr(job, "_codesign", lambda: "Identifier=x\nCDHash=abc123\n")
    job.prepare("s", trust=True, partition="cdhash")
    assert "-S apple-tool:,apple:,cdhash:abc123 " in _scripts(security)


def test_the_signature_is_read_at_the_level_that_shows_the_cdhash(security):
    """`codesign -dv` prints no CDHash, and half 3c needs it."""
    job._codesign()
    assert security[0][0][:2] == ["/usr/bin/codesign", "-dvvv"]


def test_the_partition_list_and_the_trust_entry_are_each_a_switch(security):
    job.prepare("s", trust=False, partition="none")
    script = _scripts(security)
    assert "add-generic-password" in script
    assert " -T " not in script and "partition-list" not in script


def test_a_secret_in_an_error_line_is_masked(monkeypatch):
    def run(argv, input=None, **kw):
        return SimpleNamespace(returncode=1, stdout="", stderr="unlock -p s3cret: returned 1")

    monkeypatch.setattr(job.subprocess, "run", run)
    assert job.security_script(["x"], "s3cret")["errors"] == ["unlock -p ***: returned 1"]


def _report(tmp_path, *lines) -> str:
    path = tmp_path / "report.jsonl"
    path.write_text("".join(json.dumps(x) + "\n" for x in lines), encoding="utf-8")
    return str(path)


def _arm(name, read):
    return {"arm": name, "read": {"value_equals_canary": read}}


@pytest.mark.parametrize(
    "reads, partition",
    [
        ((True, True, True), "none"),
        ((False, True, True), "unsigned"),
        ((False, False, True), "cdhash"),
        ((False, False, False), ""),
    ],
    ids=["3a", "3b", "3c", "none"],
)
def test_later_arms_use_the_half_that_read_and_none_stops_them(tmp_path, reads, partition):
    lines = [_arm(f"arm3{h}", r) for h, r in zip("abc", reads, strict=True)]
    assert show.winner(_report(tmp_path, _arm("arm1", True), *lines)) == partition


@pytest.mark.parametrize(
    "record, ok",
    [
        ({"prepare": {"rc": 0}}, True),
        ({"prepare": {"rc": 1}}, False),
        ({"failed": "OSError"}, False),
    ],
    ids=["ok", "failed-line", "failed-job"],
)
def test_a_failed_prepare_stops_the_run(tmp_path, record, ok):
    assert show.prepared(_report(tmp_path, record)) is ok


def test_an_empty_prepare_output_stops_the_run(tmp_path):
    assert show.prepared(_report(tmp_path)) is False


def test_no_winner_writes_nothing_at_all(tmp_path):
    """James's re-read of #97, N1: a lone newline passed the shell's `-s`."""
    report = _report(tmp_path, _arm("arm3a", False), _arm("arm3b", False), _arm("arm3c", False))
    r = subprocess.run(
        [sys.executable, str(PROBE / "show.py"), "--winner", report],
        capture_output=True,
        timeout=30,
    )
    assert (r.returncode, r.stdout) == (0, b"")


def test_a_failed_line_with_a_good_return_code_is_a_failed_prepare(tmp_path):
    """James's re-read of #97, N2: `security -i` returns the status of its last line."""
    record = {"prepare": {"rc": 0, "errors": ["add-generic-password: returned 45"]}}
    assert show.prepared(_report(tmp_path, record)) is False


def test_a_failed_create_stops_before_the_canary_can_land_elsewhere(monkeypatch, tmp_path):
    """Measured: `add-generic-password` with a keychain that does not exist writes to the
    default keychain without a word."""
    seen = []

    def run(argv, input=None, **kw):
        seen.append(input)
        return SimpleNamespace(returncode=1, stdout="create-keychain: returned 1", stderr="")

    monkeypatch.setattr(job.subprocess, "run", run)
    monkeypatch.setenv("HOME", str(tmp_path))
    result = job.prepare("s", trust=True, partition="none")
    assert result["errors"] == ["create-keychain: returned 1"]
    assert len(seen) == 1 and "add-generic-password" not in seen[0]


def test_a_failed_unlock_shows_on_the_line_the_owner_reads():
    line = show.line(
        {
            "arm": "arm3a",
            "mode": "drop",
            "unlock": {"rc": 1, "errors": ["unlock-keychain: returned 1"]},
            "read": {"outcome": "none", "osstatus": None, "value_equals_canary": False},
        }
    )
    assert "UNLOCK FAILED" in line


# --- the runner past its guard, against stand-ins for the system ----------------------

FAKE_LAUNCHCTL = r"""#!PYTHON
import json, os, plistlib, sys
if sys.argv[1] != "bootstrap":
    sys.exit(1 if sys.argv[1] == "print" else 0)
doc = plistlib.load(open(sys.argv[3], "rb"))
args = doc["ProgramArguments"]
open(os.environ["WALK_LOG"], "a").write(json.dumps(args) + "\n")
get = lambda flag: args[args.index(flag) + 1]
arm, action = get("--arm"), get("--action")
r = {"arm": arm, "mode": get("--mode"), "action": action, "facts": {"uid": 501}}
if action == "prepare":
    bad = arm == os.environ.get("WALK_FAIL_PREPARE")
    r["prepare"] = {"rc": 0, "errors": ["add-generic-password: returned 45"] if bad else []}
if action in ("read", "unlock-read"):
    hit = arm in os.environ.get("WALK_READS", "").split(",")
    r["read"] = {"outcome": "value", "osstatus": None, "value_equals_canary": hit}
open(doc["StandardOutPath"], "a").write(json.dumps(r) + "\n")
"""
FAKE_ID = r"""#!/bin/bash
if [ "$1" = "-u" ]; then echo 0; exit 0; fi
[ -e "$WALK_ROOT/user-exists" ]
"""
FAKE_SYSADMINCTL = r"""#!/bin/bash
if [ "$1" = "-addUser" ]; then
  touch "$WALK_ROOT/user-exists"
else
  rm -f "$WALK_ROOT/user-exists"
fi
"""
FAKE_INSTALL = r"""#!/bin/bash
while [ "${1#-}" != "$1" ]; do shift 2; done
cp "$@"
"""
FAKE_UV = r"""#!/bin/bash
for a; do last="$a"; done
if [ "$2" = "venv" ]; then
  mkdir -p "$last/bin"
  ln -s "$WALK_PYTHON" "$last/bin/python"
fi
if [ "$2" = "python" ]; then mkdir -p "$UV_PYTHON_INSTALL_DIR"; fi
"""
FAKE_TAR = r"""#!/bin/bash
for a; do dir="$a"; done
mkdir -p "$dir/uv-aarch64-apple-darwin"
cp "$WALK_ROOT/stubs/fake-uv" "$dir/uv-aarch64-apple-darwin/uv"
"""
SIMPLE_STUBS = {
    "chown": "",
    "createhomedir": 'mkdir -p "$WALK_ROOT/Users/mbxprobe"',
    "curl": "",
    "dscl": "exit 1",
    "dseditgroup": "",
    "hostname": "echo M1mini",
    "openssl": "echo abc123",
    "sfltool": "",
    "shasum": "cat >/dev/null",
    "stat": "echo german",
    "uname": "echo Darwin",
}


@pytest.fixture
def walk(tmp_path):
    """`probe.sh` with its paths moved under tmp_path and the system's tools stood in for, so
    every branch past `guard` runs, under `/bin/bash`, which is 3.2 on a Mac. This shows the
    script holds together. It says nothing about launchd, the keychain or the real tools."""
    root = tmp_path / "root"
    for d in ("Library/LaunchDaemons", "Users/Shared", "run", "stubs"):
        (root / d).mkdir(parents=True)
    text = (PROBE / "probe.sh").read_text(encoding="utf-8")
    script = root / "run" / "probe.sh"
    script.write_text(
        text.replace("/Library/", f"{root}/Library/").replace("/Users/", f"{root}/Users/"),
        encoding="utf-8",
    )
    script.chmod(0o755)
    for name in ("job.py", "show.py", "requirements.txt"):
        (root / "run" / name).write_text(
            (PROBE / name).read_text(encoding="utf-8"), encoding="utf-8"
        )
    stubs = {
        "id": FAKE_ID,
        "install": FAKE_INSTALL,
        "fake-uv": FAKE_UV,
        "launchctl": FAKE_LAUNCHCTL.replace("PYTHON", sys.executable, 1),
        "sysadminctl": FAKE_SYSADMINCTL,
        "tar": FAKE_TAR,
    }
    stubs.update({k: f"#!/bin/bash\n{v}\n" for k, v in SIMPLE_STUBS.items()})
    for name, body in stubs.items():
        path = root / "stubs" / name
        path.write_text(body, encoding="utf-8")
        path.chmod(0o755)
    env = {
        "PATH": f"{root / 'stubs'}:{os.environ['PATH']}",
        "WALK_ROOT": str(root),
        "WALK_LOG": str(root / "jobs.log"),
        "WALK_PYTHON": sys.executable,
    }

    def run(*commands, **extra):
        last = None
        for command in commands:
            last = subprocess.run(
                [str(script), command],
                capture_output=True,
                text=True,
                timeout=60,
                env={**env, **extra},
            )
            if last.returncode != 0:
                return last
        return last

    run.root = root
    run.jobs = lambda: [
        json.loads(line) for line in (root / "jobs.log").read_text(encoding="utf-8").splitlines()
    ]
    return run


def test_the_whole_sitting_walks_through_and_later_arms_use_the_half_that_read(walk):
    reads = "arm3b,arm4b,arm7,arm5"
    r = walk("setup", "arm1", "arm2", "arm3", "arm6", "arm7", "arm4-install", WALK_READS=reads)
    assert r.returncode == 0, r.stderr
    directory = walk.root / "Library/mailbox-autopilot-probe"
    (directory / "out/arm4-boot.out").write_text(
        json.dumps({"arm": "arm4", "mode": "drop", "read": {"value_equals_canary": True}}) + "\n",
        encoding="utf-8",
    )
    r = walk("arm4-read", "arm4b", "arm5-reset", "arm5-read", "report", WALK_READS=reads)
    assert r.returncode == 0, r.stderr
    assert "arm5/username" in r.stdout or '"arm5"' in r.stdout
    arms = [a[a.index("--arm") + 1] for a in walk.jobs()]
    assert arms == [
        "arm1",
        "arm1",
        "arm2-prepare",
        "arm2",
        "arm2",
        "arm3a-prepare",
        "arm3a",
        "arm3b-prepare",
        "arm3b",
        "arm3c-prepare",
        "arm3c",
        "arm6a-prepare",
        "arm6a",
        "arm6b-prepare",
        "arm6b",
        "arm7-prepare",
        "arm7",
        "arm4-prepare",
        "arm4b",
        "arm5-reset",
        "arm5",
        "arm5",
    ]
    prepared = [a for a in walk.jobs() if "prepare" in a]
    later = [a for a in prepared if a[a.index("--arm") + 1].startswith(("arm4", "arm6", "arm7"))]
    assert later and all(a[a.index("--partition") + 1] == "unsigned" for a in later)
    assert any(a[0].endswith("venv312/bin/python") for a in walk.jobs())

    r = walk("cleanup")
    assert r.returncode == 1  # the report is still there, and status says so
    assert "keychain-probe-report.jsonl exists" in r.stdout
    (walk.root / "Users/Shared/keychain-probe-report.jsonl").unlink()
    r = walk("status")
    assert r.returncode == 0 and "of the probe is left" in r.stdout


def test_no_half_of_arm_3_stops_the_run_with_the_right_reason(walk):
    r = walk("setup", "arm3")
    assert r.returncode == 2 and "no half of arm 3 read the canary" in r.stderr
    for arm in ("arm6", "arm7", "arm4-install"):
        r = walk(arm)
        assert r.returncode == 2 and "no half of arm 3 read the canary" in r.stderr


def test_a_failed_prepare_line_stops_the_run_with_the_right_reason(walk):
    r = walk("setup", "arm2", WALK_FAIL_PREPARE="arm2-prepare")
    assert r.returncode == 2 and "prepare failed" in r.stderr


def test_a_failing_sfltool_is_not_a_clean_status(walk):
    (walk.root / "stubs/sfltool").write_text("#!/bin/bash\nexit 1\n", encoding="utf-8")
    r = walk("status")
    assert r.returncode == 1 and "sfltool failed" in r.stdout


def test_a_background_task_record_is_named(walk):
    (walk.root / "stubs/sfltool").write_text(
        "#!/bin/bash\necho ai.neckarshore.keychain-probe\n", encoding="utf-8"
    )
    r = walk("status")
    assert r.returncode == 1 and "background task record" in r.stdout


@pytest.mark.parametrize("user, shaped", [(None, False), ("u", True)], ids=["drop", "username"])
def test_the_plist_is_valid_and_keeps_stderr(user, shaped):
    doc = plistlib.loads(job.plist(label="l", program=["p"], out="o", err="e", user=user))
    assert doc["RunAtLoad"] is True and doc["StandardErrorPath"] == "e"
    assert ("UserName" in doc) is shaped
