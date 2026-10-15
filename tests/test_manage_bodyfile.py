"""Where the text of a draft may be read from: the tool's own outbox folder, and nothing
else (Founder decisions 2026-10-08, after two security reviews of the compose build).

The reader is constrained because the caller is an agent that has just read mail. A free
file read plus a recipient (free in `manage compose`, the possibly hostile sender in
`manage draft`) would let one command stage any readable local file as a draft.
"""

import json
import os
import pwd
import threading

import pytest
from click.testing import CliRunner

from mailbox_cleanup.cli import cli
from mailbox_cleanup.manage import bodyfile
from mailbox_cleanup.manage.bodyfile import MAX_BODY_BYTES, ensure_outbox, read_body_file

# Bound at import, before the autouse fixture in tests/conftest.py replaces `outbox_dir`
# with the test's tmp_path. Every other test runs against that replacement, so without
# this name no test would notice a wrong production location.
from mailbox_cleanup.manage.bodyfile import outbox_dir as _REAL_OUTBOX_DIR  # noqa: E402

OUTSIDE = __file__  # a readable regular UTF-8 file that is not in the outbox


@pytest.fixture
def outbox(tmp_path, monkeypatch):
    box = tmp_path / "outbox"
    box.mkdir(mode=0o700)
    monkeypatch.setattr(bodyfile, "outbox_dir", lambda: str(box))
    return box


def _put(box, name="body.txt", text="Hello"):
    p = box / name
    p.write_text(text, encoding="utf-8")
    return p


def test_a_file_directly_in_the_outbox_is_read(outbox):
    assert read_body_file(str(_put(outbox, text="Grüße"))) == "Grüße"


def test_a_file_outside_the_outbox_is_refused(outbox, tmp_path):
    assert read_body_file(OUTSIDE) is None
    assert read_body_file(str(_put(tmp_path))) is None  # the parent of the outbox


def test_a_path_elsewhere_is_refused_even_when_the_outbox_holds_that_name(outbox, tmp_path):
    """The file is opened by name inside the outbox, so a path elsewhere could never be
    read. Without the parent check it would be answered with the outbox file of the same
    name: the caller names one file and silently gets another."""
    _put(outbox, "body.txt", "from the outbox")
    elsewhere = _put(tmp_path, "body.txt", "from elsewhere")
    assert read_body_file(str(elsewhere)) is None


def test_a_bare_file_name_is_refused(outbox, monkeypatch):
    _put(outbox)
    monkeypatch.chdir(outbox.parent)
    assert read_body_file("body.txt") is None


def test_a_file_in_a_subfolder_of_the_outbox_is_refused(outbox):
    sub = outbox / "sub"
    sub.mkdir()
    assert read_body_file(str(_put(sub))) is None


@pytest.mark.parametrize("name", ["..", ".", ""])
def test_dot_names_are_refused(outbox, name):
    assert read_body_file(os.path.join(str(outbox), name)) is None


def test_a_path_that_climbs_out_of_the_outbox_is_refused(outbox, tmp_path):
    _put(tmp_path, "secret.txt")
    assert read_body_file(str(outbox / ".." / "secret.txt")) is None


def test_a_symlink_in_the_outbox_is_refused_wherever_it_points(outbox):
    inside = _put(outbox, "real.txt")
    (outbox / "to-inside.txt").symlink_to(inside)
    (outbox / "to-outside.txt").symlink_to(OUTSIDE)
    assert read_body_file(str(outbox / "to-inside.txt")) is None
    assert read_body_file(str(outbox / "to-outside.txt")) is None


def test_an_outbox_that_is_a_symlink_is_refused(tmp_path, monkeypatch):
    real = tmp_path / "elsewhere"
    real.mkdir(mode=0o700)
    _put(real)
    link = tmp_path / "outbox"
    link.symlink_to(real, target_is_directory=True)
    monkeypatch.setattr(bodyfile, "outbox_dir", lambda: str(link))
    assert read_body_file(str(link / "body.txt")) is None


@pytest.mark.parametrize("mode", [0o755, 0o750, 0o707, 0o777])
def test_an_outbox_others_can_enter_is_refused(outbox, mode):
    p = _put(outbox)
    outbox.chmod(mode)
    try:
        assert read_body_file(str(p)) is None
    finally:
        outbox.chmod(0o700)


def test_a_missing_outbox_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(bodyfile, "outbox_dir", lambda: str(tmp_path / "nope"))
    assert read_body_file(str(tmp_path / "nope" / "body.txt")) is None


def test_the_cap_is_one_megabyte_and_bounds_what_is_read(outbox):
    assert MAX_BODY_BYTES == 1024 * 1024
    ok = outbox / "ok.txt"
    ok.write_bytes(b"a" * MAX_BODY_BYTES)
    big = outbox / "big.txt"
    big.write_bytes(b"a" * (MAX_BODY_BYTES + 1))
    assert len(read_body_file(str(ok))) == MAX_BODY_BYTES
    assert read_body_file(str(big)) is None


def test_the_cap_holds_when_the_reported_size_is_wrong(outbox, monkeypatch):
    """The cap is enforced on the bytes read, not on the size the file system reports: a
    file can grow between the two."""
    big = outbox / "big.txt"
    big.write_bytes(b"a" * (MAX_BODY_BYTES + 1))
    real_fstat = os.fstat

    class _Lying:
        def __init__(self, st):
            self._st = st

        def __getattr__(self, name):
            return 1 if name == "st_size" else getattr(self._st, name)

    monkeypatch.setattr(os, "fstat", lambda fd: _Lying(real_fstat(fd)))
    assert read_body_file(str(big)) is None


def test_a_fifo_in_the_outbox_is_refused_without_hanging(outbox):
    fifo = outbox / "body.fifo"
    os.mkfifo(fifo)
    out = {}
    t = threading.Thread(target=lambda: out.update(r=read_body_file(str(fifo))), daemon=True)
    t.start()
    t.join(timeout=5)
    if t.is_alive():
        with open(fifo, "w"):
            pass
        t.join(timeout=5)
        raise AssertionError("read_body_file blocked on a FIFO")
    assert out["r"] is None


def test_a_directory_a_binary_file_and_a_missing_file_are_refused(outbox):
    (outbox / "dir").mkdir()
    (outbox / "bin.txt").write_bytes(b"\xff\xfe\x00bad")
    assert read_body_file(str(outbox / "dir")) is None
    assert read_body_file(str(outbox / "bin.txt")) is None
    assert read_body_file(str(outbox / "missing.txt")) is None


def test_the_outbox_location_ignores_the_environment(monkeypatch, tmp_path):
    """Security review of cb0f3a2: the first rule ("temp directory only") took its root
    from TMPDIR, which the constrained caller sets. The outbox comes from the user
    database, so no variable moves it."""
    before = bodyfile._default_outbox()
    for var in ("HOME", "TMPDIR", "TEMP", "TMP", "XDG_CONFIG_HOME", "USERPROFILE"):
        monkeypatch.setenv(var, str(tmp_path))
    monkeypatch.chdir(tmp_path)
    assert bodyfile._default_outbox() == before
    home = pwd.getpwuid(os.getuid()).pw_dir
    assert before == os.path.join(home, ".mailbox-cleanup", "outbox")


def test_ensure_outbox_creates_a_private_folder_once(tmp_path, monkeypatch):
    box = tmp_path / "cfg" / "outbox"
    monkeypatch.setattr(bodyfile, "outbox_dir", lambda: str(box))
    assert ensure_outbox() == str(box)
    assert box.is_dir() and (box.stat().st_mode & 0o777) == 0o700
    _put(box)
    assert ensure_outbox() == str(box)  # a second call keeps what is there
    assert read_body_file(str(box / "body.txt")) == "Hello"


def test_cli_outbox_reports_the_folder_and_touches_no_account(tmp_path, monkeypatch):
    box = tmp_path / "cfg" / "outbox"
    monkeypatch.setattr(bodyfile, "outbox_dir", lambda: str(box))
    res = CliRunner().invoke(cli, ["manage", "outbox", "--json"])
    assert res.exit_code == 0, res.output
    out = json.loads(res.output)
    assert out["ok"] is True and out["subcommand"] == "manage.outbox"
    assert out["path"] == str(box) and box.is_dir()
    assert out["max_bytes"] == MAX_BODY_BYTES


def test_cli_outbox_says_so_when_the_folder_is_not_private(tmp_path, monkeypatch):
    box = tmp_path / "outbox"
    box.mkdir(mode=0o755)
    box.chmod(0o755)
    monkeypatch.setattr(bodyfile, "outbox_dir", lambda: str(box))
    res = CliRunner().invoke(cli, ["manage", "outbox", "--json"])
    assert res.exit_code == 4, res.output
    assert json.loads(res.output)["error_code"] == "outbox_unusable"
    assert (box.stat().st_mode & 0o777) == 0o755  # never changed silently


@pytest.mark.parametrize(
    "cmd",
    [["draft", "--uid", "7", "--no-quote"], ["compose", "--to", "a@example.org", "--subject", "s"]],
)
def test_both_drafting_commands_read_from_the_outbox_only(cmd, outbox, tmp_path, monkeypatch):
    """`manage draft` is held to the same rule as `manage compose`: its recipient is the
    sender of the answered mail, which in the attack case is the attacker."""
    from mailbox_cleanup.auth import Credentials
    from mailbox_cleanup.config import Account
    from mailbox_cleanup.manage import cli as mcli

    monkeypatch.setenv("MAILBOX_CLEANUP_AUDIT_LOG", str(tmp_path / "audit.log"))
    monkeypatch.setattr(
        mcli,
        "resolve_account_and_credentials",
        lambda **kw: (
            Account(alias="t", email="test@localhost", server="imap.example.com", port=993),
            Credentials(email="test@localhost", password="x", server="imap.example.com"),
        ),
    )

    def _boom(*a, **kw):
        raise AssertionError("imap_connect was called")

    monkeypatch.setattr(mcli, "imap_connect", _boom)
    res = CliRunner().invoke(cli, ["manage", *cmd, "--body-file", OUTSIDE, "--json"])
    assert res.exit_code == 4, res.output
    out = json.loads(res.output)
    assert out["error_code"] == "bad_args" and "manage outbox" in out["message"]
    assert OUTSIDE not in res.output


def test_the_shipped_outbox_is_the_folder_from_the_user_database():
    """Corruption probe that made this test necessary: `outbox_dir()` changed to return
    the temp directory, and all 754 tests stayed green."""
    home = pwd.getpwuid(os.getuid()).pw_dir
    assert _REAL_OUTBOX_DIR() == os.path.join(home, ".mailbox-cleanup", "outbox")
    assert _REAL_OUTBOX_DIR() == bodyfile._default_outbox()


def test_a_hard_link_to_an_existing_file_is_refused(outbox, tmp_path):
    """A hard link is a regular file and no symlink, so it passes every other check, and
    linking copies nothing. A file written fresh for a draft has exactly one name."""
    secret = tmp_path / "secret.txt"
    secret.write_text("PRIVATE KEY", encoding="utf-8")
    linked = outbox / "body.txt"
    os.link(secret, linked)
    assert read_body_file(str(linked)) is None
    secret.unlink()  # one name left: it is an ordinary file again
    assert read_body_file(str(linked)) == "PRIVATE KEY"
