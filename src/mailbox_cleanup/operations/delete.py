"""Delete operation — soft-delete (move to Trash) with dry-run by default."""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from ..folders import resolve_folder
from .batching import move_in_batches
from .selection import select_messages


class PreviewMismatchError(RuntimeError):
    """The set to act on differs in size from the one the user confirmed."""

    def __init__(self, expected: int, found: int):
        self.expected = expected
        self.found = found
        super().__init__(
            f"preview showed {expected} messages, {found} match now; nothing was moved. "
            "Run the dry-run again and confirm the new count."
        )


def check_expected(expect_count: int | None, found: int) -> None:
    if expect_count is not None and expect_count != found:
        raise PreviewMismatchError(expect_count, found)


@dataclass
class DeleteResult:
    affected_uids: list[str]
    dry_run: bool
    target_folder: str | None
    folder: str
    sample: list[dict]
    kept_count: int = 0
    kept_by_config: int = 0
    by_sender: dict[str, int] = field(default_factory=dict)


def sample_of(msgs) -> list[dict]:
    return [
        {"uid": m.uid, "from": m.from_, "subject": m.subject, "date": str(m.date)} for m in msgs[:5]
    ]


def run_delete(
    mb,
    *,
    folder: str = "INBOX",
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
) -> DeleteResult:
    """Find matching messages, move to Trash if apply=True. Otherwise dry-run.

    With apply, `expect_count` binds the action to the confirmed preview: if the set
    changed size since then, nothing moves (PreviewMismatchError).
    """
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
    target = resolve_folder(mb, "trash")
    if apply:
        check_expected(expect_count, len(uids))
        if uids:
            if not target:
                raise RuntimeError(
                    "Could not resolve Trash folder on server (no SPECIAL-USE, no fallback match)."
                )
            move_in_batches(mb, uids, target)
    return DeleteResult(
        affected_uids=uids,
        dry_run=not apply,
        target_folder=target,
        folder=folder,
        sample=sample_of(sel.messages),
        kept_count=sel.kept_count,
        kept_by_config=sel.kept_by_config,
        by_sender=sel.by_sender,
    )
