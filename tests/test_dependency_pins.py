"""Every direct dependency is pinned exactly, to the version uv.lock resolves (#80 item 6).

The estate rule is exact versions. A range in pyproject.toml lets a fresh lock pick up a
release nobody read; a pin that disagrees with uv.lock means the lock was not regenerated.

Release condition 3 (James on #90) adds the build backend and the uv that CI runs: uv
builds this project outside the lock on every machine that runs bin/mailbox-autopilot,
so hatchling and its dependencies are pinned too, and CI installs one exact uv.
"""

import re
import tomllib
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = tomllib.loads((ROOT / "pyproject.toml").read_text())
LOCK = tomllib.loads((ROOT / "uv.lock").read_text())
WORKFLOWS = sorted((ROOT / ".github" / "workflows").glob("*.yml"))

# The uv that CI installs. The bot host is bound to the same version by the launcher
# (#76, launcher spec), not by pyproject.toml (see test_pyproject_requires_no_uv_version).
UV = "0.13.5"
# uv 0.13.5 writes lock revision 5. uv 0.12.13 writes revision 3 on a fresh lock, and
# `uv lock --check` and `uv sync --locked` under 0.13.5 accept that lock (measured
# 2026-10-29), so this file is the only place that sees it.
LOCK_REVISION = 5
# The dependencies of the pinned hatchling, measured with
# `echo hatchling==1.33.0 | uv pip compile - --python-version 3.11` (2026-10-29).
# A hatchling bump measures them again.
HATCHLING = "1.33.0"
HATCHLING_DEPENDENCIES = {"packaging", "pathspec", "pluggy", "tomlkit", "trove-classifiers"}

PIN = re.compile(r"^(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)==(?P<version>[0-9][0-9A-Za-z.+!-]*)$")


def _normalise(name):
    return re.sub(r"[-_.]+", "-", name).lower()


def _direct_requirements():
    project = PYPROJECT["project"]
    reqs = list(project["dependencies"])
    for extra in project.get("optional-dependencies", {}).values():
        reqs.extend(extra)
    return reqs


def _locked_versions():
    return {_normalise(p["name"]): p["version"] for p in LOCK["package"]}


def test_there_are_direct_requirements_to_check():
    assert len(_direct_requirements()) >= 8


@pytest.mark.parametrize("requirement", _direct_requirements())
def test_every_direct_requirement_is_an_exact_pin(requirement):
    assert PIN.match(requirement), f"{requirement!r} is not pinned with == to one version"


@pytest.mark.parametrize("requirement", _direct_requirements())
def test_every_pin_equals_the_locked_version(requirement):
    match = PIN.match(requirement)
    assert match, f"{requirement!r} is not pinned with == to one version"
    name = _normalise(match["name"])
    locked = _locked_versions()
    assert name in locked, f"{name} is not in uv.lock"
    assert match["version"] == locked[name], (
        f"{name} is pinned to {match['version']} but uv.lock resolves {locked[name]}"
    )


def test_the_lock_was_regenerated_after_the_pins():
    """uv.lock records the specifiers it resolved from. If they differ from pyproject.toml,
    `uv run --frozen` (bin/mailbox-autopilot) runs a lock that no longer matches the pins."""
    root = next(p for p in LOCK["package"] if p["name"] == PYPROJECT["project"]["name"])
    recorded = {(_normalise(r["name"]), r["specifier"]) for r in root["metadata"]["requires-dist"]}
    declared = set()
    for requirement in _direct_requirements():
        match = PIN.match(requirement)
        assert match, f"{requirement!r} is not pinned with == to one version"
        declared.add((_normalise(match["name"]), f"=={match['version']}"))
    assert recorded == declared


def _build_requires():
    return list(PYPROJECT["build-system"]["requires"])


def _build_constraints():
    return list(PYPROJECT.get("tool", {}).get("uv", {}).get("build-constraint-dependencies", []))


def _pinned(requirements):
    pins = {}
    for requirement in requirements:
        match = PIN.match(requirement)
        assert match, f"{requirement!r} is not pinned with == to one version"
        pins[_normalise(match["name"])] = match["version"]
    return pins


def test_the_build_backend_is_pinned_exactly():
    """uv resolves [build-system].requires outside the lock; under the launcher that
    install runs as root (James on #90, release condition 3)."""
    assert _pinned(_build_requires()) == {"hatchling": HATCHLING}


def test_the_build_backends_dependencies_are_pinned_as_build_constraints():
    assert set(_pinned(_build_constraints())) == HATCHLING_DEPENDENCIES


def test_the_lock_records_the_build_constraints():
    """`uv lock --check` in CI compares this record with pyproject.toml."""
    recorded = {
        (_normalise(c["name"]), c["specifier"])
        for c in LOCK.get("manifest", {}).get("build-constraints", [])
    }
    declared = {(name, f"=={v}") for name, v in _pinned(_build_constraints()).items()}
    assert recorded == declared


def test_the_lock_was_written_by_the_pinned_uv():
    assert (LOCK["version"], LOCK["revision"]) == (1, LOCK_REVISION)


def _workflow_steps():
    for workflow in WORKFLOWS:
        for job in (yaml.safe_load(workflow.read_text()).get("jobs") or {}).values():
            yield from job.get("steps") or []


def test_ci_installs_one_exact_uv():
    """Without a version, setup-uv installs the latest uv, which can read and write the
    lock differently from the uv that wrote it."""
    steps = [
        s for s in _workflow_steps() if str(s.get("uses", "")).startswith("astral-sh/setup-uv@")
    ]
    assert len(steps) >= 2
    assert {str((s.get("with") or {}).get("version")) for s in steps} == {UV}


def test_ci_never_relocks_on_its_own():
    """A plain `uv sync` rewrites a lock that no longer matches pyproject.toml and then
    tests against a lock nobody committed. --locked refuses instead."""
    syncs = [
        line.strip()
        for s in _workflow_steps()
        for line in str(s.get("run", "")).splitlines()
        if line.strip().startswith("uv sync")
    ]
    assert syncs
    assert all("--locked" in line.split() for line in syncs), syncs


def test_pyproject_requires_no_uv_version():
    """The Founder's decision of 2026-10-29 (#76, release condition 3): bin/mailbox-autopilot
    runs on every plugin user's uv, and uv 0.12.13 runs this lock under --frozen. A
    required-version would refuse those users and protect them from nothing. CI is bound
    by its pin, the bot host by the launcher."""
    assert "required-version" not in PYPROJECT.get("tool", {}).get("uv", {})
