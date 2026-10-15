"""Parse and execute List-Unsubscribe per RFC 2369 / RFC 8058."""

import ipaddress
import re
import socket
from dataclasses import dataclass
from urllib.parse import urlparse

import requests

from .filters import build_imap_search
from .selection import _is_kept, parse_keep

_LINK_RE = re.compile(r"<([^>]+)>")

_ALLOWED_SCHEMES = ("http", "https")


class EgressBlocked(Exception):
    """Raised when an email-derived outbound target is refused by the SSRF guard."""


def _is_blocked_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Deny loopback, private, link-local, reserved, multicast and unspecified ranges.

    Covers the classic SSRF targets: 127.0.0.0/8, 10/172.16/192.168 RFC-1918,
    169.254.0.0/16 (incl. the 169.254.169.254 cloud-metadata endpoint), etc.
    IPv4-mapped IPv6 (::ffff:127.0.0.1) is unwrapped before the check.
    """
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return (
        ip.is_loopback
        or ip.is_private
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def validate_egress_url(url: str) -> None:
    """Fail-closed SSRF guard for email-controlled URLs.

    Policy: **scheme + IP-range denylist** (NOT a host allowlist — legitimate
    unsubscribe endpoints live on arbitrary sender domains). Refuses:
      - any scheme other than http/https,
      - a missing host,
      - a host that resolves (via DNS) to a private/loopback/link-local/
        reserved/multicast/unspecified address. ALL resolved addresses are
        checked, so a domain that rebinds to an internal IP is still refused.

    Raises ``EgressBlocked`` on refusal; returns ``None`` when the target is
    permitted. Callers must still disable HTTP redirects to close the
    redirect-to-internal bypass.
    """
    parsed = urlparse(url)
    if parsed.scheme not in _ALLOWED_SCHEMES:
        raise EgressBlocked(f"scheme not allowed: {parsed.scheme!r}")
    host = parsed.hostname
    if not host:
        raise EgressBlocked(f"missing host in target: {url!r}")
    try:
        infos = socket.getaddrinfo(host, parsed.port or None, proto=socket.IPPROTO_TCP)
    except socket.gaierror as e:
        raise EgressBlocked(f"DNS resolution failed for {host!r}: {e}") from e
    if not infos:
        raise EgressBlocked(f"no addresses resolved for {host!r}")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if _is_blocked_ip(ip):
            raise EgressBlocked(f"host {host!r} resolves to non-routable address {ip}")


# perform_unsubscribe's info string for a target the egress guard refused.
EGRESS_REFUSED_PREFIX = "blocked by egress guard:"


@dataclass
class UnsubAction:
    kind: str  # "https" or "mailto"
    target: str  # URL or mail address
    one_click: bool  # only relevant for https


def parse_list_unsubscribe(
    *,
    list_unsubscribe: str,
    list_unsubscribe_post: str | None,
) -> list[UnsubAction]:
    actions: list[UnsubAction] = []
    one_click = bool(
        list_unsubscribe_post and "List-Unsubscribe=One-Click" in list_unsubscribe_post
    )
    for raw in _LINK_RE.findall(list_unsubscribe or ""):
        raw = raw.strip()
        scheme = raw.lower()  # URI schemes are case-insensitive (RFC 3986 §3.1)
        if scheme.startswith("mailto:"):
            target = raw[len("mailto:") :].split("?", 1)[0]
            actions.append(UnsubAction(kind="mailto", target=target, one_click=False))
        elif scheme.startswith(("http://", "https://")):
            actions.append(UnsubAction(kind="https", target=raw, one_click=one_click))
    # Prefer https first, then mailto
    actions.sort(key=lambda a: 0 if a.kind == "https" else 1)
    return actions


def perform_unsubscribe(action: UnsubAction, *, timeout: float = 15.0) -> tuple[bool, str]:
    """Execute an HTTPS unsubscribe. `mailto:` is never executed (spec §5): the package
    contains no send code, so mailto-only senders are reported for manual handling."""
    if action.kind == "https":
        # SSRF guard: refuse email-controlled URLs pointing at internal ranges
        # or non-http(s) schemes BEFORE any outbound request is made.
        try:
            validate_egress_url(action.target)
        except EgressBlocked as e:
            return False, f"{EGRESS_REFUSED_PREFIX} {e}"
        try:
            # allow_redirects=False closes the redirect-to-internal bypass:
            # a permitted public host cannot 30x us onto 169.254.169.254 etc.
            if action.one_click:
                resp = requests.post(
                    action.target,
                    data="List-Unsubscribe=One-Click",
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                    timeout=timeout,
                    allow_redirects=False,
                )
            else:
                resp = requests.get(action.target, timeout=timeout, allow_redirects=False)
            return resp.status_code < 400, f"HTTP {resp.status_code}"
        except Exception as e:
            return False, f"HTTPS error: {e}"
    if action.kind == "mailto":
        return False, "manual: mailto-only unsubscribe is not supported"
    return False, f"Unknown action kind: {action.kind}"


def collect_unsub_targets(mb, *, sender: str, folder: str = "INBOX", keep=()) -> dict:
    """Find messages from sender, parse their List-Unsubscribe headers, return targets.

    Messages from a kept sender are left out entirely: neither their UIDs (which
    would go to Trash) nor their unsubscribe targets are returned.
    """
    addrs, domains = parse_keep(keep)
    mb.folder.set(folder)
    msgs = list(
        mb.fetch(
            build_imap_search(sender=sender),
            headers_only=True,
            mark_seen=False,
            bulk=True,
        )
    )
    seen: set[tuple[str, str]] = set()
    out: list[dict] = []
    uids: list[str] = []
    kept = 0
    for m in msgs:
        if _is_kept(m.from_, addrs, domains):
            kept += 1
            continue
        if m.uid:
            uids.append(m.uid)
        headers = m.headers or {}
        lu = headers.get("list-unsubscribe") or headers.get("List-Unsubscribe")
        lup = headers.get("list-unsubscribe-post") or headers.get("List-Unsubscribe-Post")
        if not lu:
            continue
        lu_val = lu[0] if isinstance(lu, tuple) else lu
        lup_val = (lup[0] if isinstance(lup, tuple) else lup) if lup else None
        for action in parse_list_unsubscribe(
            list_unsubscribe=lu_val,
            list_unsubscribe_post=lup_val,
        ):
            key = (action.kind, action.target)
            if key in seen:
                continue
            seen.add(key)
            out.append(
                {
                    "kind": action.kind,
                    "target": action.target,
                    "one_click": action.one_click,
                }
            )
    return {"uids": uids, "actions": out, "kept_count": kept}
