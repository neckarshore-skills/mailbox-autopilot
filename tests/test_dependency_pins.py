"""Every direct dependency is pinned exactly, to the version uv.lock resolves (#80 item 6).

The estate rule is exact versions. A range in pyproject.toml lets a fresh lock pick up a
release nobody read; a pin that disagrees with uv.lock means the lock was not regenerated.
"""

import re
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = tomllib.loads((ROOT / "pyproject.toml").read_text())
LOCK = tomllib.loads((ROOT / "uv.lock").read_text())

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
