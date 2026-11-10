"""The pre-push hook runs the leak guard on every commit a push would publish.

Each test builds a throwaway repository with a local bare `origin` and calls the hook
the way git does (refs on stdin). Nothing here touches a real remote. The synthetic
address is assembled at run time so this file itself never carries one.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
HOOK = ROOT / ".githooks" / "pre-push"
ZERO = "0" * 40
SYNTHETIC = "jane.doe" + "@" + "some-company.test"

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")


def _git(repo, *args):
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.com",
    }
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, env=env, capture_output=True, text=True
    ).stdout.strip()


def _commit(repo, name, text, msg):
    (repo / name).write_text(text, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", msg)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path):
    origin, work = tmp_path / "origin.git", tmp_path / "work"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(work)], check=True)
    (work / "scripts").mkdir()
    shutil.copy(ROOT / "scripts" / "leak_guard.py", work / "scripts" / "leak_guard.py")
    _git(work, "remote", "add", "origin", str(origin))
    base = _commit(work, "README.md", "hello\n", "base")
    _git(work, "push", "-q", "origin", "main")  # origin now has `base`
    return work, base


def _run_hook(work, local_sha, remote_sha):
    env = {k: v for k, v in os.environ.items() if k != "LEAK_GUARD_PRIVATE_LIST"}
    stdin = f"refs/heads/main {local_sha} refs/heads/main {remote_sha}\n"
    return subprocess.run(
        ["bash", str(HOOK), "origin", "unused"],
        cwd=work,
        input=stdin,
        capture_output=True,
        text=True,
        env=env,
    )


def test_a_clean_push_passes(repo):
    work, base = repo
    head = _commit(work, "a.py", "x = 'anna@example.com'\n", "clean")
    r = _run_hook(work, head, base)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "green on 1 new commit(s)" in r.stdout


def test_an_address_in_a_pushed_commit_refuses_the_push(repo):
    work, base = repo
    head = _commit(work, "notes.md", f"contact {SYNTHETIC}\n", "leak")
    r = _run_hook(work, head, base)
    assert r.returncode == 1
    assert "notes.md:1: email" in r.stdout
    assert "push refused" in r.stderr


def test_an_address_removed_again_later_still_refuses_the_push(repo):
    """The net diff is clean, but the first commit is published with the push."""
    work, base = repo
    _commit(work, "notes.md", f"contact {SYNTHETIC}\n", "leak")
    head = _commit(work, "notes.md", "contact removed\n", "remove")
    r = _run_hook(work, head, base)
    assert r.returncode == 1


def test_a_new_branch_scans_only_what_origin_lacks(repo):
    work, _ = repo
    _git(work, "checkout", "-q", "-b", "obi/x")
    head = _commit(work, "b.py", "y = 2\n", "on branch")
    r = _run_hook(work, head, ZERO)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "green on 1 new commit(s)" in r.stdout


def test_a_branch_deletion_scans_nothing(repo):
    work, base = repo
    r = _run_hook(work, ZERO, base)
    assert r.returncode == 0
    assert "green on 0 new commit(s)" in r.stdout


def test_a_missing_guard_script_fails_closed(repo):
    work, base = repo
    (work / "scripts" / "leak_guard.py").unlink()
    head = _commit(work, "c.py", "z = 3\n", "no guard")
    r = _run_hook(work, head, base)
    assert r.returncode == 1
    assert "not found" in r.stderr
