"""Dedupe operation — group by Message-ID, keep oldest, move rest to Trash."""

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime

from ..folders import resolve_folder
from .batching import move_in_batches
from .delete import check_expected


@dataclass
class DedupeResult:
    dry_run: bool
    folder: str
    target_folder: str | None
    groups: list[dict] = field(default_factory=list)
    duplicate_uids: list[str] = field(default_factory=list)


def run_dedupe(
    mb, *, folder: str = "INBOX", apply: bool = False, expect_count: int | None = None
) -> DedupeResult:
    """With apply, `expect_count` binds the action to the confirmed preview (#55)."""
    target = resolve_folder(mb, "trash")
    mb.folder.set(folder)
    msgs = list(mb.fetch(headers_only=True, mark_seen=False, bulk=True))

    by_id: dict[str, list[tuple[datetime, str]]] = defaultdict(list)
    for m in msgs:
        if not m.uid:
            continue
        headers = m.headers or {}
        # imap-tools yields headers as {key: tuple(values)}
        mid_tuple = headers.get("message-id") or headers.get("Message-ID")
        if not mid_tuple:
            continue
        mid = mid_tuple[0] if isinstance(mid_tuple, tuple) else mid_tuple
        date = m.date if isinstance(m.date, datetime) else datetime.now(UTC)
        date = date if date.tzinfo else date.replace(tzinfo=UTC)
        by_id[mid].append((date, m.uid))

    groups: list[dict] = []
    drop_uids: list[str] = []
    for mid, entries in by_id.items():
        if len(entries) < 2:
            continue
        entries.sort()  # oldest first
        keep = entries[0][1]
        drops = [uid for _, uid in entries[1:]]
        groups.append({"message_id": mid, "keep": keep, "drop": drops})
        drop_uids.extend(drops)

    if apply:
        check_expected(expect_count, len(drop_uids))
    if apply and drop_uids:
        if not target:
            raise RuntimeError("Could not resolve Trash folder.")
        move_in_batches(mb, drop_uids, target)

    return DedupeResult(
        dry_run=not apply,
        folder=folder,
        target_folder=target,
        groups=groups,
        duplicate_uids=drop_uids,
    )
