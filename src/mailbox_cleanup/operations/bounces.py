"""Bounces operation — find bounce / auto-reply messages, optionally move to Trash."""

from collections.abc import Iterable
from dataclasses import dataclass

from ..classify import is_bounce
from ..folders import resolve_folder
from .batching import move_in_batches
from .delete import check_expected
from .selection import _is_kept, parse_keep


@dataclass
class BouncesResult:
    dry_run: bool
    folder: str
    target_folder: str | None
    affected_uids: list[str]
    sample: list[dict]
    kept_count: int = 0


def run_bounces(
    mb,
    *,
    folder: str = "INBOX",
    apply: bool = False,
    keep: Iterable[str] = (),
    expect_count: int | None = None,
) -> BouncesResult:
    """With apply, `expect_count` binds the action to the confirmed preview (#55)."""
    addrs, domains = parse_keep(keep)
    target = resolve_folder(mb, "trash")
    mb.folder.set(folder)
    msgs = list(mb.fetch(headers_only=True, mark_seen=False, bulk=True))
    matched = [
        m
        for m in msgs
        if is_bounce(from_addr=(m.from_ or ""), subject=(m.subject or ""), headers={})
    ]
    kept = [m for m in matched if _is_kept(m.from_, addrs, domains)]
    matched = [m for m in matched if m not in kept]
    uids = [m.uid for m in matched if m.uid]
    sample = [{"uid": m.uid, "from": m.from_, "subject": m.subject} for m in matched[:5]]
    if apply:
        check_expected(expect_count, len(uids))
    if apply and uids:
        if not target:
            raise RuntimeError("Could not resolve Trash folder.")
        move_in_batches(mb, uids, target)
    return BouncesResult(
        dry_run=not apply,
        folder=folder,
        target_folder=target,
        affected_uids=uids,
        sample=sample,
        kept_count=len(kept),
    )
