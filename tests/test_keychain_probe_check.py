"""check.py holds the probe's report against the run sheet's expected column, arm by arm.

Sitting 1 (2026-11-03) said `none` for arm 2 where the sheet expected locked, and only a
person reading the log noticed. These tests pin that a deviation is named by the script."""

import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PROBE = ROOT / "scripts" / "keychain_probe"
SHEET = ROOT / "docs" / "2026-11-02-keychain-probe-runsheet.md"

spec = importlib.util.spec_from_file_location("keychain_probe_check", PROBE / "check.py")
check = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check)


def _read(arm, outcome="none", canary=False, mode="drop", console="german", status=None):
    return {
        "arm": arm,
        "mode": mode,
        "action": "read",
        "facts": {"uid": 502, "console_user": console},
        "search": {"domain": "user", "on_list": True},
        "read": {"outcome": outcome, "osstatus": status, "value_equals_canary": canary},
    }


def _sitting(**override):
    """A whole sitting that matches the sheet; `override` replaces records by arm name."""
    records = {
        "arm1/drop": _read("arm1"),
        "arm1/username": _read("arm1", mode="username"),
        "arm2/drop": _read("arm2", "KeyringLocked", status=-25308),
        "arm2/username": _read("arm2", "KeyringLocked", mode="username", status=-25308),
        "arm3a/drop": _read("arm3a", "KeyringError", status=-25293),
        "arm3b/drop": _read("arm3b", "value", canary=True),
        "arm3c/drop": _read("arm3c", "value", canary=True),
        "arm6a/drop": _read("arm6a", "KeyringError", status=-25293),
        "arm6b/drop": _read("arm6b", "KeyringError", status=-25293),
        "arm7/drop": _read("arm7"),
        "arm4/drop": _read("arm4", "value", canary=True, console=None),
        "arm4b/drop": _read("arm4b", "value", canary=True),
        "arm5/drop": _read("arm5", "KeyringLocked", status=-25308),
        "arm5/username": _read("arm5", "KeyringLocked", mode="username", status=-25308),
    }
    records.update(override)
    return [r for r in records.values() if r is not None]


def _report(tmp_path, records):
    path = tmp_path / "report.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    return str(path)


def _verdicts(records):
    return {name: verdict for verdict, name, _ in check.judge(records)}


def test_a_sitting_that_matches_the_sheet_has_no_deviation():
    verdicts = _verdicts(_sitting())
    assert "DEVIATES" not in verdicts.values()
    assert "OPEN" not in verdicts.values()


def test_sitting_1_arm_2_would_have_been_flagged(tmp_path):
    """The real case: arm 2 off the search list, sitting 1's probe defect."""
    off = _read("arm2")
    off["search"] = {"domain": "user", "on_list": False}
    del off["read"]
    assert _verdicts(_sitting(**{"arm2/drop": off}))["arm2/drop"] == "DEVIATES"


@pytest.mark.parametrize(
    "key, record",
    [
        ("arm1/drop", _read("arm1", "value", canary=True)),
        ("arm4/drop", _read("arm4", "KeyringLocked", console=None)),
        ("arm4b/drop", _read("arm4b", "KeyringLocked")),
        ("arm4b/drop", _read("arm4b", "value", canary=True, console="mbxprobe")),
        ("arm5/drop", _read("arm5", "value", canary=True)),
        ("arm5/drop", _read("arm5", "none")),
        ("arm6b/drop", _read("arm6b", "value", canary=True)),
        ("arm7/drop", _read("arm7", "KeyringLocked")),
        ("arm7/drop", _read("arm7", "value", canary=True)),
    ],
    ids=[
        "1-canary",
        "4-locked",
        "4b-locked",
        "4b-not-german",
        "5-canary",
        "5-none",
        "6b-canary",
        "7-locked",
        "7-canary",
    ],
)
def test_each_expected_class_is_enforced(key, record):
    assert _verdicts(_sitting(**{key: record}))[key] == "DEVIATES"


def test_arm_3_deviates_only_when_no_half_read():
    none_read = {f"arm3{h}/drop": _read(f"arm3{h}", "KeyringError", status=-25293) for h in "abc"}
    assert _verdicts(_sitting(**none_read))["arm3"] == "DEVIATES"
    assert _verdicts(_sitting())["arm3"] == "MATCHES"


def test_arm_2_outcome_is_reported_not_judged():
    """The sheet calls a locked item's outcome in such a job unmeasured: say it, do not fail."""
    verdicts = _verdicts(_sitting(**{"arm2/drop": _read("arm2", "none")}))
    assert verdicts["arm2/drop"] == "MATCHES"
    lines = [t for _, n, t in check.judge(_sitting()) if n == "arm2/drop"]
    assert "KeyringLocked" in lines[0]


def test_a_failed_job_deviates():
    failed = {"arm": "arm6a", "mode": "drop", "failed": "no output"}
    assert _verdicts(_sitting(**{"arm6a/drop": failed}))["arm6a/drop"] == "DEVIATES"


def test_a_missing_arm_is_open_not_a_deviation():
    verdicts = _verdicts(_sitting(**{"arm5/drop": None, "arm5/username": None}))
    assert verdicts["arm5/drop"] == "OPEN"
    assert "DEVIATES" not in verdicts.values()


def test_prepare_search_and_reset_records_are_not_judged():
    extra = [
        {"arm": "arm2-prepare", "mode": "drop", "action": "prepare", "prepare": {"rc": 0}},
        {
            "arm": "arm2-search-user",
            "mode": "drop",
            "action": "search",
            "search": {"domain": "user", "on_list": False},
        },
        {"arm": "arm5-reset", "mode": "drop", "action": "reset", "reset": {"rc": 0}},
    ]
    names = [n for _, n, _ in check.judge(_sitting() + extra)]
    assert not [n for n in names if "prepare" in n or "search" in n or "reset" in n]


def test_the_exit_code_is_1_on_a_deviation_and_0_otherwise(tmp_path):
    def run(records):
        return subprocess.run(
            [sys.executable, str(PROBE / "check.py"), _report(tmp_path, records)],
            capture_output=True,
            text=True,
        )

    good = run(_sitting())
    assert good.returncode == 0, good.stdout
    bad = run(_sitting(**{"arm7/drop": _read("arm7", "value", canary=True)}))
    assert bad.returncode == 1
    assert "DEVIATES arm7/drop" in bad.stdout


def test_the_judged_arms_are_exactly_the_reads_the_runner_makes():
    """Both ways: a renamed arm in probe.sh, or a new read nobody judges, must show."""
    runner = (PROBE / "probe.sh").read_text(encoding="utf-8")
    made = set(re.findall(r"read_job(?:_with \"\$PY2\")? (arm\w+) (drop|username)", runner))
    made |= set(re.findall(r"run_job (arm\w+) (drop|username) read", runner))  # arm 1, no list
    made |= set(re.findall(r"record (arm4) (drop)", runner))  # arm 4 runs at boot
    judged = {tuple(k.split("/")) for k in [*check.EXPECTED, *check.ARM3]}
    assert made == judged


def test_the_sheet_names_the_check():
    assert "check.py" in SHEET.read_text(encoding="utf-8")


def test_the_check_reads_no_value():
    """The report never holds the value; the check must not start reading one either."""
    source = (PROBE / "check.py").read_text(encoding="utf-8")
    assert "get_password" not in source and "keyring" not in source
