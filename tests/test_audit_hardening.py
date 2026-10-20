"""Audit-log hardening (#75): owner-only file modes, rotation at a size cap, and
argument keys instead of values, for cleanup and manage records alike."""

import json
import os
import stat

import pytest
from click.testing import CliRunner

from mailbox_cleanup import audit
from mailbox_cleanup.audit import AUDIT_LOG_PATH_ENV, log_action, log_manage_action
from mailbox_cleanup.cli import cli

CANARY = "canary-7f3a@leak.example"


def _mode(path):
    return stat.S_IMODE(path.stat().st_mode)


@pytest.fixture
def tmp_audit(tmp_path, monkeypatch):
    log_path = tmp_path / "audit.log"
    monkeypatch.setenv(AUDIT_LOG_PATH_ENV, str(log_path))
    return log_path


@pytest.fixture
def default_audit(tmp_path, monkeypatch):
    """The default location, redirected into tmp_path: no env override set."""
    log_path = tmp_path / "home" / ".mailbox-cleanup" / "audit.log"
    monkeypatch.delenv(AUDIT_LOG_PATH_ENV, raising=False)
    monkeypatch.setattr(audit, "DEFAULT_AUDIT_LOG", log_path)
    return log_path


def _cleanup_record(**overrides):
    kw = dict(
        subcommand="delete",
        account="work",
        args={"sender": [CANARY], "older_than": None},
        folder="INBOX",
        affected_uids=["1"],
        result="success",
    )
    kw.update(overrides)
    log_action(**kw)


def _manage_record():
    log_manage_action(
        subcommand="manage search",
        account="work",
        folder="INBOX",
        uids=["1"],
        result="success",
        arg_keys=["sender"],
    )


# --- file and directory modes ---------------------------------------------------


@pytest.mark.parametrize("write", [_cleanup_record, _manage_record])
def test_a_fresh_log_is_owner_only(tmp_audit, write):
    old = os.umask(0o022)  # the common default, under which open() would give 0644
    try:
        write()
    finally:
        os.umask(old)
    assert _mode(tmp_audit) == 0o600


@pytest.mark.parametrize("write", [_cleanup_record, _manage_record])
def test_an_existing_world_readable_log_is_repaired_on_write(tmp_audit, write):
    tmp_audit.write_text("")
    tmp_audit.chmod(0o644)
    write()
    assert _mode(tmp_audit) == 0o600


def test_the_default_directory_is_owner_only(default_audit):
    default_audit.parent.mkdir(parents=True)
    default_audit.parent.chmod(0o755)
    _cleanup_record()
    assert _mode(default_audit.parent) == 0o700
    assert _mode(default_audit) == 0o600


def test_the_parent_of_an_override_path_is_left_alone(tmp_path, monkeypatch):
    shared = tmp_path / "shared"
    shared.mkdir()
    shared.chmod(0o755)
    monkeypatch.setenv(AUDIT_LOG_PATH_ENV, str(shared / "audit.log"))
    _cleanup_record()
    assert _mode(shared) == 0o755


def test_any_cli_run_repairs_the_modes_without_writing(default_audit):
    """A dry run writes no record; the repair must not depend on a write."""
    default_audit.parent.mkdir(parents=True)
    default_audit.parent.chmod(0o755)
    default_audit.write_text('{"old": 1}\n')
    default_audit.chmod(0o644)
    rotated = default_audit.with_name("audit.log.1")
    rotated.write_text("")
    rotated.chmod(0o644)
    r = CliRunner().invoke(cli, ["config", "--help"])
    assert r.exit_code == 0, r.output
    assert _mode(default_audit.parent) == 0o700
    assert _mode(default_audit) == 0o600
    assert _mode(rotated) == 0o600
    assert default_audit.read_text() == '{"old": 1}\n'


def test_the_repair_without_any_log_creates_nothing(default_audit):
    CliRunner().invoke(cli, ["config", "--help"])
    assert not default_audit.parent.exists()


# --- rotation ------------------------------------------------------------------


def test_the_log_rotates_at_the_size_cap(tmp_audit, monkeypatch):
    monkeypatch.setattr(audit, "MAX_BYTES", 200)
    monkeypatch.setattr(audit, "BACKUP_COUNT", 2)
    for _ in range(12):
        _cleanup_record()
    rotated = sorted(p.name for p in tmp_audit.parent.glob("audit.log*"))
    assert rotated == ["audit.log", "audit.log.1", "audit.log.2"]
    for p in tmp_audit.parent.glob("audit.log*"):
        assert _mode(p) == 0o600
        assert p.stat().st_size <= 200 + 400  # one record may straddle the cap
        for line in p.read_text().splitlines():
            json.loads(line)  # rotation never splits a record


def test_rotation_keeps_the_newest_record_in_the_live_file(tmp_audit, monkeypatch):
    monkeypatch.setattr(audit, "MAX_BYTES", 200)
    for i in range(6):
        _cleanup_record(affected_uids=[str(i)])
    last = json.loads(tmp_audit.read_text().splitlines()[-1])
    assert last["affected_uids"] == ["5"]


# --- keys, not values ------------------------------------------------------------


def test_cleanup_records_keys_only(tmp_audit):
    log_action(
        subcommand="delete",
        account="work",
        args={
            "sender": [CANARY],
            "subject_contains": "canary-subject",
            "recipient": None,
            "keep": [],
            "keep_config": [CANARY, "@bank.example"],
            "older_than": "6m",
        },
        folder="INBOX",
        affected_uids=["1"],
        result="success",
    )
    text = tmp_audit.read_text()
    assert CANARY not in text
    assert "canary-subject" not in text
    assert "bank.example" not in text
    rec = json.loads(text)
    assert "args" not in rec
    assert rec["arg_keys"] == ["older_than", "sender", "subject_contains"]
    assert rec["keep_config_count"] == 2
