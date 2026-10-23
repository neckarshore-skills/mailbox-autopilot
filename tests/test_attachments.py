import json
from datetime import UTC, datetime
from unittest.mock import MagicMock

from click.testing import CliRunner

from mailbox_cleanup.cli import cli
from mailbox_cleanup.operations.attachments import find_large_messages


def _msg(uid, size, date):
    m = MagicMock()
    m.uid = uid
    m.from_ = "x@y.example"
    m.subject = "huge"
    m.size = size
    m.date = date
    return m


def test_find_large_messages_size_only():
    msgs = [
        _msg("1", 5_000_000, datetime(2025, 1, 1, tzinfo=UTC)),
        _msg("2", 15_000_000, datetime(2025, 1, 1, tzinfo=UTC)),
        _msg("3", 25_000_000, datetime(2025, 1, 1, tzinfo=UTC)),
    ]
    res = find_large_messages(msgs, size_gt_bytes=10 * 1024 * 1024)
    assert sorted(m.uid for m in res) == ["2", "3"]


def test_find_large_messages_with_age_filter():
    now = datetime(2026, 5, 4, tzinfo=UTC)
    msgs = [
        _msg("1", 15_000_000, datetime(2025, 1, 1, tzinfo=UTC)),
        _msg("2", 15_000_000, datetime(2026, 4, 1, tzinfo=UTC)),
    ]
    res = find_large_messages(msgs, size_gt_bytes=10 * 1024 * 1024, older_than="6m", now=now)
    assert [m.uid for m in res] == ["1"]


def test_attachments_cli_lists_only_no_strip(seeded_mailbox, monkeypatch, tmp_path):
    g = seeded_mailbox
    from mailbox_cleanup import cli_helpers
    from mailbox_cleanup.auth import Credentials
    from mailbox_cleanup.config import (
        DEFAULT_CONFIG_PATH_ENV,
        Account,
        Config,
        save_config,
    )

    cfg_path = tmp_path / "cfg.json"
    monkeypatch.setenv(DEFAULT_CONFIG_PATH_ENV, str(cfg_path))
    monkeypatch.delenv("MAILBOX_CLEANUP_ACCOUNT", raising=False)
    save_config(
        Config(
            default="test",
            accounts=(Account(alias="test", email="test@local", server=g["host"], port=g["port"]),),
        )
    )

    fake_creds = lambda email: Credentials(  # noqa: E731
        email=g["user"], password=g["password"], server=g["host"]
    )
    monkeypatch.setattr(cli_helpers, "get_credentials", fake_creds)
    monkeypatch.setenv("MAILBOX_CLEANUP_AUDIT_LOG", str(tmp_path / "audit.log"))

    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "attachments",
            "--email",
            "test",
            "--size-gt=1b",  # all fixture mails will exceed
            "--json",
        ],
    )
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert "candidates" in data
    assert data["dry_run"] is True
