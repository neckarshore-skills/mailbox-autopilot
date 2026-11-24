"""#76 L11: the path route against the real Security framework.

Runs only where PROBE_REAL_KEYCHAIN=1 is set on macOS: the macOS CI job, a throwaway VM.
Never on a laptop's own keychain, because `create-keychain` changes the user's search list.

What this shows: `job.read_at` builds a query the framework accepts, reads an item of a
keychain by its path that is not on the search list, keeps the framework's status, and
which partition half lets this interpreter read. What it cannot show: anything about a job
in launchd's system session, which is what L11 asks and only the target Mac can answer.

Every read runs in its own process with a timeout: a runner logged in at a GUI could show
an access prompt, and a prompt nobody answers must end the read, not the job. The statuses
are printed for the CI log (`pytest -s`), as evidence for James's read."""

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PROBE = ROOT / "scripts" / "keychain_probe"

pytestmark = pytest.mark.skipif(
    sys.platform != "darwin" or os.environ.get("PROBE_REAL_KEYCHAIN") != "1",
    reason="real keychain: macOS CI job only (PROBE_REAL_KEYCHAIN=1)",
)

spec = importlib.util.spec_from_file_location("keychain_probe_job_real", PROBE / "job.py")
job = importlib.util.module_from_spec(spec)
spec.loader.exec_module(job)

SECRET = "ci-throwaway-unlock"  # a throwaway keychain on a throwaway VM


def _security(*lines: str) -> dict:
    """Through `security -i` like the probe, so the unlock secret stays out of argv."""
    return job.security_script(list(lines), SECRET)


def _read(path: str, service: str = job.SERVICE) -> dict:
    code = (
        "import importlib.util, json, sys\n"
        f"spec = importlib.util.spec_from_file_location('j', {str(PROBE / 'job.py')!r})\n"
        "j = importlib.util.module_from_spec(spec); spec.loader.exec_module(j)\n"
        "print(json.dumps(j.read_at(sys.argv[1], sys.argv[2])))\n"
    )
    try:
        r = subprocess.run(
            [sys.executable, "-c", code, path, service],
            capture_output=True,
            text=True,
            timeout=20,
        )
    except subprocess.TimeoutExpired:
        return {"outcome": "timeout", "osstatus": None, "value_equals_canary": False}
    assert job.CANARY not in r.stdout + r.stderr, "the value reached the output"
    if r.returncode:
        print("READ CRASHED:", r.stderr[-800:])  # the canary was checked absent above
        return {"outcome": "crashed", "osstatus": r.returncode, "value_equals_canary": False}
    return json.loads(r.stdout)


@pytest.fixture
def keychain(tmp_path):
    path = str(tmp_path / "probe.keychain-db")
    created = _security(f"create-keychain -p {SECRET} {path}")
    # As in job.prepare: no add runs after a failed create, or it lands in the default keychain.
    assert created == {"rc": 0, "errors": []}, created
    # Off the search list, so a read can reach it by path only.
    _, listed = job._run(job.SECURITY, "list-keychains", "-d", "user")
    kept = [k for k in job._keychains(listed) if not k.endswith("probe.keychain-db")]
    _security(f"list-keychains -d user -s {' '.join(kept)}")
    yield path
    _security(f"delete-keychain {path}")


def _item(path: str, partition: str) -> dict:
    lines = [
        f"unlock-keychain -p {SECRET} {path}",
        f"add-generic-password -s {job.SERVICE} -a {job.ACCOUNT} -w {job.CANARY} "
        f"-T {job._python()} {path}",
    ]
    if partition != "none":
        lines.append(
            f"set-generic-password-partition-list -S {job._partitions(partition)} "
            f"-s {job.SERVICE} -a {job.ACCOUNT} -k {SECRET} {path}"
        )
    return _security(*lines)


def test_the_path_route_reads_a_keychain_that_is_not_on_the_search_list(keychain):
    _, effective = job._run(job.SECURITY, "list-keychains")
    assert keychain not in effective, "the fixture left it on the list; this proves nothing"
    results = {}
    for partition in ("none", "unsigned", "cdhash"):
        _security(f"delete-generic-password -s {job.SERVICE} -a {job.ACCOUNT} {keychain}")
        added = _item(keychain, partition)
        assert added == {"rc": 0, "errors": []}, added
        results[partition] = _read(keychain)
    print("PATH ROUTE, unlocked, per partition half:", json.dumps(results, sort_keys=True))
    assert any(r["value_equals_canary"] for r in results.values()), results


def test_the_path_route_keeps_not_found_as_the_frameworks_status(keychain):
    _item(keychain, "cdhash")
    read = _read(keychain, service="mailbox-cleanup-probe-wrong")
    print("PATH ROUTE, wrong service:", json.dumps(read))
    assert read == {"outcome": "status", "osstatus": -25300, "value_equals_canary": False}


def test_the_path_route_on_a_locked_keychain_does_not_read(keychain):
    _item(keychain, "cdhash")
    _security(f"lock-keychain {keychain}")
    read = _read(keychain)
    print("PATH ROUTE, locked:", json.dumps(read))
    # Without prompts off this waited for a dialog until the timeout (first green run). With
    # them off the framework answered errSecAuthFailed, -25293, not the -25308 expected.
    assert read == {"outcome": "status", "osstatus": -25293, "value_equals_canary": False}


def test_the_path_route_names_a_keychain_that_is_not_there(tmp_path):
    read = _read(str(tmp_path / "absent.keychain-db"))
    print("PATH ROUTE, no such keychain:", json.dumps(read))
    assert read["outcome"] == "status" and read["osstatus"] != 0
