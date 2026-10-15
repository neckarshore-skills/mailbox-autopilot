import json
import socket
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from mailbox_cleanup import cli as cli_mod
from mailbox_cleanup.auth import Credentials
from mailbox_cleanup.config import Account
from mailbox_cleanup.operations.unsubscribe import (
    EgressBlocked,
    UnsubAction,
    parse_list_unsubscribe,
    perform_unsubscribe,
    validate_egress_url,
)


def _addrinfo(ip: str, port: int = 443):
    family = socket.AF_INET6 if ":" in ip else socket.AF_INET
    return [(family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (ip, port))]


def test_parse_https_only():
    actions = parse_list_unsubscribe(
        list_unsubscribe="<https://example.com/unsub?t=abc>",
        list_unsubscribe_post=None,
    )
    assert any(a.kind == "https" and a.target == "https://example.com/unsub?t=abc" for a in actions)


def test_parse_mailto_only():
    actions = parse_list_unsubscribe(
        list_unsubscribe="<mailto:unsub@example.com?subject=unsubscribe>",
        list_unsubscribe_post=None,
    )
    assert any(a.kind == "mailto" and a.target == "unsub@example.com" for a in actions)


def test_parse_both_https_preferred():
    actions = parse_list_unsubscribe(
        list_unsubscribe="<mailto:u@example.com>, <https://example.com/unsub>",
        list_unsubscribe_post="List-Unsubscribe=One-Click",
    )
    https = [a for a in actions if a.kind == "https"][0]
    assert https.one_click is True


def test_perform_https_one_click_uses_post():
    action = UnsubAction(kind="https", target="https://example.com/unsub", one_click=True)
    with (
        patch("mailbox_cleanup.operations.unsubscribe.requests") as r,
        patch("mailbox_cleanup.operations.unsubscribe.socket.getaddrinfo") as gai,
    ):
        gai.return_value = _addrinfo("93.184.216.34")  # public host
        r.post.return_value = MagicMock(status_code=200)
        ok, info = perform_unsubscribe(action)
    assert ok is True
    r.post.assert_called_once()
    assert "List-Unsubscribe=One-Click" in r.post.call_args.kwargs["data"]


def test_perform_https_get_when_no_one_click():
    action = UnsubAction(kind="https", target="https://example.com/unsub", one_click=False)
    with (
        patch("mailbox_cleanup.operations.unsubscribe.requests") as r,
        patch("mailbox_cleanup.operations.unsubscribe.socket.getaddrinfo") as gai,
    ):
        gai.return_value = _addrinfo("93.184.216.34")  # public host
        r.get.return_value = MagicMock(status_code=200)
        ok, info = perform_unsubscribe(action)
    assert ok is True
    r.get.assert_called_once()


def test_perform_mailto_sends_nothing_and_reports_manual():
    action = UnsubAction(kind="mailto", target="unsub@example.com", one_click=False)
    ok, info = perform_unsubscribe(action)
    assert ok is False
    assert info.startswith("manual:")


def test_parse_mailto_only_still_listed():
    actions = parse_list_unsubscribe(
        list_unsubscribe="<mailto:unsub@example.com?subject=unsubscribe>",
        list_unsubscribe_post=None,
    )
    assert [(a.kind, a.target) for a in actions] == [("mailto", "unsub@example.com")]


# --- SSRF egress guard (fail-closed) ---------------------------------------


def test_validate_egress_refuses_cloud_metadata_ip():
    # AWS/GCP link-local metadata endpoint — the canonical SSRF target.
    with pytest.raises(EgressBlocked):
        validate_egress_url("http://169.254.169.254/latest/meta-data/")


def test_validate_egress_refuses_non_http_scheme():
    with pytest.raises(EgressBlocked):
        validate_egress_url("file:///etc/passwd")


def test_validate_egress_allows_public_host():
    with patch("mailbox_cleanup.operations.unsubscribe.socket.getaddrinfo") as gai:
        gai.return_value = _addrinfo("93.184.216.34")
        # Must not raise.
        validate_egress_url("https://newsletter.example.com/unsub?t=abc")


def test_perform_https_refuses_loopback_literal():
    action = UnsubAction(kind="https", target="http://127.0.0.1/admin", one_click=False)
    with patch("mailbox_cleanup.operations.unsubscribe.requests") as r:
        ok, info = perform_unsubscribe(action)
    assert ok is False
    assert "blocked" in info.lower()
    r.get.assert_not_called()
    r.post.assert_not_called()


def test_perform_https_refuses_private_range_literal():
    action = UnsubAction(kind="https", target="http://192.168.1.1/", one_click=False)
    with patch("mailbox_cleanup.operations.unsubscribe.requests") as r:
        ok, info = perform_unsubscribe(action)
    assert ok is False
    r.get.assert_not_called()


def test_perform_https_refuses_metadata_ip():
    action = UnsubAction(
        kind="https", target="http://169.254.169.254/latest/meta-data/", one_click=True
    )
    with patch("mailbox_cleanup.operations.unsubscribe.requests") as r:
        ok, info = perform_unsubscribe(action)
    assert ok is False
    r.post.assert_not_called()


def test_perform_https_refuses_dns_rebind_to_private():
    # Hostname looks public but resolves to loopback → must be refused.
    action = UnsubAction(kind="https", target="https://evil.example.com/unsub", one_click=False)
    with (
        patch("mailbox_cleanup.operations.unsubscribe.requests") as r,
        patch("mailbox_cleanup.operations.unsubscribe.socket.getaddrinfo") as gai,
    ):
        gai.return_value = _addrinfo("127.0.0.1")
        ok, info = perform_unsubscribe(action)
    assert ok is False
    r.get.assert_not_called()


def test_perform_https_disables_redirects():
    action = UnsubAction(kind="https", target="https://example.com/unsub", one_click=False)
    with (
        patch("mailbox_cleanup.operations.unsubscribe.requests") as r,
        patch("mailbox_cleanup.operations.unsubscribe.socket.getaddrinfo") as gai,
    ):
        gai.return_value = _addrinfo("93.184.216.34")
        r.get.return_value = MagicMock(status_code=200)
        perform_unsubscribe(action)
    assert r.get.call_args.kwargs.get("allow_redirects") is False


# --- CLI `unsubscribe --apply` (Review Focus 1) -----------------------------


def _run_apply(monkeypatch, actions, flags=("--apply", "--json"), exit_code=0):
    """Run `unsubscribe --apply` against a fake mailbox; return (payload, moves, audit)."""
    moved = []
    audit = []

    class _MB:
        def move(self, uids, folder):
            moved.append((tuple(uids), folder))

    @contextmanager
    def _fake_connect(creds, port=993):
        yield _MB()

    monkeypatch.setattr(
        cli_mod,
        "resolve_account_and_credentials",
        lambda **kw: (
            Account(alias="t", email="t@example.com", server="imap.example.com"),
            Credentials(email="t@example.com", password="x", server="imap.example.com"),
        ),
    )
    monkeypatch.setattr(cli_mod, "imap_connect", _fake_connect)
    monkeypatch.setattr(
        cli_mod,
        "collect_unsub_targets",
        lambda mb, sender, folder, keep=(): {"uids": ["7"], "actions": actions},
    )
    monkeypatch.setattr(cli_mod, "resolve_folder", lambda mb, kind: "Trash")
    monkeypatch.setattr(cli_mod, "log_action", lambda **kw: audit.append(kw))
    res = CliRunner().invoke(cli_mod.cli, ["unsubscribe", "--sender", "news@example.com", *flags])
    assert res.exit_code == exit_code, res.output
    if "--json" not in flags:
        return res.output, moved, audit
    return json.loads(res.output), moved, audit


def test_apply_mailto_only_keeps_mail_and_lists_manual(monkeypatch):
    payload, moved, audit = _run_apply(
        monkeypatch, [{"kind": "mailto", "target": "unsub@example.com", "one_click": False}]
    )
    assert payload["manual_unsubscribe"] == ["unsub@example.com"]
    assert moved == []
    # Review Focus 1: the audit result is not "success", and no mail counts as affected.
    assert [(a["result"], a["affected_uids"]) for a in audit] == [("manual", [])]


def test_apply_https_and_mailto_runs_https_trashes_and_lists_nothing(monkeypatch):
    monkeypatch.setattr(cli_mod, "perform_unsubscribe", lambda a: (True, "HTTP 200"))
    payload, moved, audit = _run_apply(
        monkeypatch,
        [
            {"kind": "https", "target": "https://example.com/u", "one_click": True},
            {"kind": "mailto", "target": "unsub@example.com", "one_click": False},
        ],
    )
    assert payload["manual_unsubscribe"] == []
    assert moved == [(("7",), "Trash")]
    assert [a["result"] for a in audit] == ["success"]


def test_apply_without_list_unsubscribe_header_still_trashes(monkeypatch):
    payload, moved, audit = _run_apply(monkeypatch, [])
    assert payload["manual_unsubscribe"] == []
    assert moved == [(("7",), "Trash")]
    assert [a["result"] for a in audit] == ["success"]


HTTPS_ONLY = [{"kind": "https", "target": "https://example.com/u", "one_click": True}]


def test_apply_https_failure_keeps_mail_and_lists_target(monkeypatch):
    # Founder 2026-10-08 18:02 CEST, "Behalten und auflisten":
    # a failed HTTPS unsubscribe must not look like a successful one.
    monkeypatch.setattr(cli_mod, "perform_unsubscribe", lambda a: (False, "HTTP 500"))
    payload, moved, audit = _run_apply(monkeypatch, HTTPS_ONLY, exit_code=5)
    assert moved == []
    # A failed attempt is a failure for any caller that checks status (CodeRabbit on #68).
    assert payload["ok"] is False
    assert payload["error_code"] == "unsubscribe_failed"
    assert payload["manual_unsubscribe"] == ["https://example.com/u"]
    assert payload["refused_unsubscribe"] == []
    assert [(a["result"], a["affected_uids"]) for a in audit] == [("failed", [])]


def test_apply_https_failure_also_lists_mailto_alternative(monkeypatch):
    monkeypatch.setattr(cli_mod, "perform_unsubscribe", lambda a: (False, "HTTP 404"))
    payload, moved, _ = _run_apply(
        monkeypatch,
        [*HTTPS_ONLY, {"kind": "mailto", "target": "unsub@example.com", "one_click": False}],
        exit_code=5,
    )
    assert moved == []
    assert payload["manual_unsubscribe"] == ["https://example.com/u", "unsub@example.com"]


def test_apply_refused_link_is_never_offered_for_manual_handling(monkeypatch):
    # A link the egress guard refused (private range, loopback, metadata IP, rebind)
    # must not be handed to a person to open: it is listed apart, and the mail is kept.
    monkeypatch.setattr(
        cli_mod,
        "perform_unsubscribe",
        lambda a: (False, "blocked by egress guard: host '10.0.0.1' resolves to x"),
    )
    actions = [
        {"kind": "https", "target": "https://10.0.0.1/u", "one_click": True},
        {"kind": "mailto", "target": "unsub@example.com", "one_click": False},
    ]
    payload, moved, audit = _run_apply(monkeypatch, actions, exit_code=5)
    assert moved == []
    assert payload["manual_unsubscribe"] == ["unsub@example.com"]
    assert payload["refused_unsubscribe"] == ["https://10.0.0.1/u"]
    assert [(a["result"], a["affected_uids"]) for a in audit] == [("failed", [])]


def test_apply_refused_link_text_output_warns_not_to_open(monkeypatch):
    monkeypatch.setattr(
        cli_mod, "perform_unsubscribe", lambda a: (False, "blocked by egress guard: x")
    )
    out, moved, _ = _run_apply(
        monkeypatch,
        [{"kind": "https", "target": "https://10.0.0.1/u", "one_click": True}],
        flags=("--apply",),
        exit_code=5,
    )
    assert moved == []
    by_hand = [ln for ln in out.splitlines() if ln.startswith("Unsubscribe by hand")]
    assert by_hand == []
    assert "do not open" in out and "https://10.0.0.1/u" in out


def test_apply_https_failure_text_output_says_mail_kept(monkeypatch):
    monkeypatch.setattr(cli_mod, "perform_unsubscribe", lambda a: (False, "HTTP 500"))
    out, moved, _ = _run_apply(monkeypatch, HTTPS_ONLY, flags=("--apply",), exit_code=5)
    assert moved == []
    assert "FAILED" in out
    assert "all mail kept" in out
    assert "https://example.com/u" in out


def test_dry_run_https_lists_nothing_manual(monkeypatch):
    # A dry run has not failed anything yet; the manual list stays empty.
    payload, moved, audit = _run_apply(monkeypatch, HTTPS_ONLY, flags=("--json",))
    assert payload["ok"] is True
    assert payload["manual_unsubscribe"] == []
    assert moved == [] and audit == []


MAILTO_ONLY = [{"kind": "mailto", "target": "unsub@example.com", "one_click": False}]


def test_dry_run_mailto_only_lists_manual_and_touches_nothing(monkeypatch):
    payload, moved, audit = _run_apply(monkeypatch, MAILTO_ONLY, flags=("--json",))
    assert payload["dry_run"] is True
    assert payload["manual_unsubscribe"] == ["unsub@example.com"]
    assert moved == []
    assert audit == []


def test_apply_mailto_only_text_output_does_not_claim_it_performed(monkeypatch):
    out, moved, _ = _run_apply(monkeypatch, MAILTO_ONLY, flags=("--apply",))
    assert "Performed" not in out
    assert "unsub@example.com" in out
    assert moved == []


@pytest.mark.parametrize(
    "header, kind",
    [
        ("<MAILTO:unsub@example.com?subject=unsubscribe>", "mailto"),
        ("<Mailto:unsub@example.com>", "mailto"),
        ("<HTTPS://example.com/unsub>", "https"),
    ],
)
def test_parse_scheme_is_case_insensitive(header, kind):
    """A mailto-only sender must never fall through to the no-header case, which trashes
    the mail and reports success while the sender stays subscribed (Review Focus 1)."""
    actions = parse_list_unsubscribe(list_unsubscribe=header, list_unsubscribe_post=None)
    assert [a.kind for a in actions] == [kind]


def test_apply_failed_https_text_output_reports_failure(monkeypatch):
    monkeypatch.setattr(cli_mod, "perform_unsubscribe", lambda a: (False, "HTTP 500"))
    out, moved, audit = _run_apply(
        monkeypatch,
        [{"kind": "https", "target": "https://example.com/u", "one_click": True}],
        flags=("--apply",),
        exit_code=5,
    )
    assert "Performed" not in out
    assert "FAILED" in out and "HTTP 500" in out
    # Founder 2026-10-08: the mail is kept, so the record is "failed", not "partial".
    assert moved == []
    assert [a["result"] for a in audit] == ["failed"]


def test_dry_run_mailto_only_text_output_does_not_promise_an_attempt(monkeypatch):
    out, _, _ = _run_apply(monkeypatch, MAILTO_ONLY, flags=())
    assert "Would attempt" not in out
    assert "unsub@example.com" in out
