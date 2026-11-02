"""#76 L11: smoke tests for the keychain probe. The probe itself runs only on the target
Mac, with the owner at the keyboard, so these hold what can be held anywhere: it refuses
everywhere else, its secrets stay out of argv, and the value it reads is never printed."""

import importlib.util
import json
import os
import plistlib
import re
import subprocess
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


def test_the_unlock_secret_goes_to_security_on_stdin_never_in_argv(security):
    job.prepare("s3cret", trust=True, partition="unsigned")
    argv, script = security[0]
    assert argv == [job.SECURITY, "-i"] and "s3cret" not in " ".join(argv)
    assert "create-keychain -p s3cret" in script
    assert "-S apple-tool:,apple:,unsigned: " in script


def test_the_cdhash_half_names_this_interpreter(security, monkeypatch):
    """James on #97, section 2: an ad-hoc signed binary may get a `cdhash:` partition."""
    monkeypatch.setattr(job, "_codesign", lambda: "Identifier=x\nCDHash=abc123\n")
    job.prepare("s", trust=True, partition="cdhash")
    assert "-S apple-tool:,apple:,cdhash:abc123 " in security[0][1]


def test_the_signature_is_read_at_the_level_that_shows_the_cdhash(security):
    """`codesign -dv` prints no CDHash, and half 3c needs it."""
    job._codesign()
    assert security[0][0][:2] == ["/usr/bin/codesign", "-dvvv"]


def test_the_partition_list_and_the_trust_entry_are_each_a_switch(security):
    job.prepare("s", trust=False, partition="none")
    script = security[0][1]
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


@pytest.mark.parametrize("user, shaped", [(None, False), ("u", True)], ids=["drop", "username"])
def test_the_plist_is_valid_and_keeps_stderr(user, shaped):
    doc = plistlib.loads(job.plist(label="l", program=["p"], out="o", err="e", user=user))
    assert doc["RunAtLoad"] is True and doc["StandardErrorPath"] == "e"
    assert ("UserName" in doc) is shaped
