from datetime import UTC, datetime, timedelta

from mailbox_cleanup.operations.filters import build_imap_search, parse_age


def test_parse_age_days():
    assert parse_age("30d") == timedelta(days=30)


def test_parse_age_weeks():
    assert parse_age("2w") == timedelta(weeks=2)


def test_parse_age_months_approx_30d():
    assert parse_age("3m") == timedelta(days=90)


def test_parse_age_years_approx_365d():
    assert parse_age("2y") == timedelta(days=730)


def test_parse_age_invalid():
    import pytest

    with pytest.raises(ValueError):
        parse_age("foobar")


def test_build_imap_search_sender_only():
    q = build_imap_search(sender="newsletter@x.example")
    assert "FROM" in str(q)


def test_build_imap_search_combined():
    now = datetime(2026, 5, 4, tzinfo=UTC)
    q = build_imap_search(
        sender="x@y.example",
        subject_contains="invoice",
        older_than="3m",
        now=now,
    )
    s = str(q)
    assert "FROM" in s
    assert "SUBJECT" in s
    assert "BEFORE" in s


def test_build_imap_search_no_filters_raises():
    import pytest

    with pytest.raises(ValueError):
        build_imap_search()
