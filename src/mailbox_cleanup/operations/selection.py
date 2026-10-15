"""Shared message selection for delete and move (#50).

Two stages: an IMAP search on the server (senders, subject, recipient, age), then
client-side filters on the fetched headers (category, keep-list). `limit` is applied
after both, so "the first N newsletters" means N newsletters, not N searched messages.
"""

from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from ..classify import is_automated, is_bounce, is_newsletter
from ..config import normalize_keep
from ..scan import _flatten_headers
from .filters import build_imap_search

CATEGORIES = {
    "automated": is_automated,
    "bounce": is_bounce,
    "newsletter": is_newsletter,
}


def parse_keep(entries: Iterable[str]) -> tuple[frozenset[str], frozenset[str]]:
    """Split keep entries into exact addresses and domains ("@bank.example").

    Validation is shared with the configured keep list (config.normalize_keep).
    """
    addrs: set[str] = set()
    domains: set[str] = set()
    for e in normalize_keep(entries):
        local, _at, domain = e.partition("@")
        if local:
            addrs.add(e)
        else:
            domains.add(domain)
    return frozenset(addrs), frozenset(domains)


def _is_kept(addr: str, addrs: frozenset[str], domains: frozenset[str]) -> bool:
    a = (addr or "").strip().lower()
    if a in addrs:
        return True
    domain = a.rpartition("@")[2]
    # A kept domain also keeps its subdomains: keeping too much is the safe direction.
    return any(domain == d or domain.endswith("." + d) for d in domains)


@dataclass
class Selection:
    messages: list
    kept_count: int = 0
    kept_by_config: int = 0
    by_sender: dict[str, int] = field(default_factory=dict)


def select_messages(
    mb,
    *,
    folder: str,
    sender: str | Sequence[str] | None = None,
    subject_contains: str | None = None,
    older_than: str | None = None,
    recipient: str | None = None,
    category: str | None = None,
    keep: Iterable[str] = (),
    config_keep: Iterable[str] = (),
    limit: int | None = None,
) -> Selection:
    if category is not None and category not in CATEGORIES:
        raise ValueError(f"unknown category {category!r}; one of {', '.join(sorted(CATEGORIES))}")
    addrs, domains = parse_keep(keep)
    c_addrs, c_domains = parse_keep(config_keep)
    criteria = build_imap_search(
        sender=sender,
        subject_contains=subject_contains,
        older_than=older_than,
        recipient=recipient,
        category=category,
    )
    client_side = category is not None or bool(addrs or domains or c_addrs or c_domains)
    mb.folder.set(folder)
    msgs = list(
        mb.fetch(
            criteria,
            headers_only=True,
            mark_seen=False,
            limit=None if client_side else limit,
            bulk=True,
        )
    )
    kept = 0
    kept_by_config = 0
    selected = []
    for m in msgs:
        if not m.uid:
            continue
        if category is not None:
            headers = _flatten_headers(getattr(m, "headers", None))
            if not CATEGORIES[category](
                from_addr=m.from_ or "", subject=m.subject or "", headers=headers
            ):
                continue
        if _is_kept(m.from_, c_addrs, c_domains):
            kept += 1
            kept_by_config += 1
            continue
        if _is_kept(m.from_, addrs, domains):
            kept += 1
            continue
        selected.append(m)
    if client_side and limit is not None:
        selected = selected[:limit]
    by_sender = Counter((m.from_ or "").strip().lower() for m in selected)
    return Selection(
        messages=selected,
        kept_count=kept,
        kept_by_config=kept_by_config,
        by_sender=dict(by_sender.most_common()),
    )
