"""Move operation — same filter set as delete, but explicit target folder."""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from .batching import move_in_batches
from .delete import check_expected, sample_of
from .selection import select_messages


@dataclass
class MoveResult:
    affected_uids: list[str]
    dry_run: bool
    target_folder: str
    folder: str
    sample: list[dict]
    kept_count: int = 0
    kept_by_config: int = 0
    by_sender: dict[str, int] = field(default_factory=dict)


def run_move(
    mb,
    *,
    folder: str,
    target: str,
    sender: str | Sequence[str] | None = None,
    subject_contains: str | None = None,
    older_than: str | None = None,
    recipient: str | None = None,
    category: str | None = None,
    keep: Iterable[str] = (),
    config_keep: Iterable[str] = (),
    apply: bool = False,
    limit: int | None = None,
    expect_count: int | None = None,
) -> MoveResult:
    sel = select_messages(
        mb,
        folder=folder,
        sender=sender,
        subject_contains=subject_contains,
        older_than=older_than,
        recipient=recipient,
        category=category,
        keep=keep,
        config_keep=config_keep,
        limit=limit,
    )
    uids = [m.uid for m in sel.messages]
    if apply:
        check_expected(expect_count, len(uids))
        if uids:
            move_in_batches(mb, uids, target)
    return MoveResult(
        affected_uids=uids,
        dry_run=not apply,
        target_folder=target,
        folder=folder,
        sample=sample_of(sel.messages),
        kept_count=sel.kept_count,
        kept_by_config=sel.kept_by_config,
        by_sender=sel.by_sender,
    )
