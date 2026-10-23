import json
from datetime import UTC, datetime
from unittest.mock import MagicMock

from click.testing import CliRunner

from mailbox_cleanup.cli import SCHEMA_VERSION, cli
from mailbox_cleanup.scan import build_report


def _msg(from_addr, subject, size, msg_id, date, headers=None):
    m = MagicMock()
    m.from_ = from_addr
    m.subject = subject
    m.size = size
    m.uid = str(hash((msg_id,)))[-6:]
    m.headers = {k.lower(): (v,) for k, v in (headers or {}).items()}
    m.date = date
    return m


def test_build_report_counts_categories():
    msgs = [
        _msg(
            "newsletter@linkedin.com",
            "weekly",
            5000,
            "<n1@x>",
            datetime(2025, 1, 1, tzinfo=UTC),
            headers={"list-unsubscribe": "<https://x>"},
        ),
        _msg(
            "newsletter@linkedin.com",
            "weekly",
            5000,
            "<n2@x>",
            datetime(2025, 2, 1, tzinfo=UTC),
            headers={"list-unsubscribe": "<https://x>"},
        ),
        _msg(
            "MAILER-DAEMON@ionos.de",
            "Undelivered Mail",
            2000,
            "<b1@x>",
            datetime(2025, 3, 1, tzinfo=UTC),
        ),
        _msg(
            "alice@example.com", "lunch", 15_000_000, "<plain@x>", datetime(2026, 1, 1, tzinfo=UTC)
        ),
    ]
    report = build_report(msgs, folder="INBOX", now=datetime(2026, 5, 4, tzinfo=UTC))

    assert report["schema_version"] == SCHEMA_VERSION
    assert report["folder"] == "INBOX"
    assert report["total_messages"] == 4
    assert report["categories"]["newsletters"]["count"] == 2
    assert report["categories"]["bounces_and_autoreplies"]["count"] == 1
    assert report["categories"]["large_attachments"]["count"] == 1
    assert report["categories"]["large_attachments"]["size_mb"] >= 14
    assert report["categories"]["by_year"]["2025"] == 3
    assert report["categories"]["by_year"]["2026"] == 1


def test_build_report_top_senders_sorted():
    msgs = []
    for i in range(5):
        msgs.append(
            _msg(
                "a@news.example",
                "x",
                1000,
                f"<a{i}>",
                datetime(2025, 1, 1, tzinfo=UTC),
                headers={"list-unsubscribe": "<https://x>"},
            )
        )
    for i in range(2):
        msgs.append(
            _msg(
                "b@news.example",
                "x",
                1000,
                f"<b{i}>",
                datetime(2025, 1, 1, tzinfo=UTC),
                headers={"list-unsubscribe": "<https://x>"},
            )
        )
    report = build_report(msgs, folder="INBOX", now=datetime(2026, 5, 4, tzinfo=UTC))
    senders = report["categories"]["newsletters"]["top_senders"]
    assert senders[0]["sender"] == "a@news.example"
    assert senders[0]["count"] == 5
    assert senders[1]["sender"] == "b@news.example"
    assert senders[1]["count"] == 2


def test_scan_cli_emits_json(seeded_mailbox, monkeypatch, tmp_path):
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

    def fake_get_credentials(email):
        return Credentials(email=g["user"], password=g["password"], server=g["host"])

    monkeypatch.setattr(cli_helpers, "get_credentials", fake_get_credentials)

    runner = CliRunner()
    result = runner.invoke(cli, ["scan", "--email", "test", "--json"])
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["total_messages"] == 4
    assert data["categories"]["newsletters"]["count"] >= 1
    assert data["categories"]["bounces_and_autoreplies"]["count"] >= 1
