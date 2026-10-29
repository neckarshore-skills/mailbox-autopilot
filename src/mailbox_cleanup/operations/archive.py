"""Archive operation — bulk-move old messages into Archive/YYYY subfolders."""

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime

from imap_tools import AND

from ..folders import resolve_folder
from .batching import move_in_batches
from .delete import check_expected
from .filters import parse_age


@dataclass
class ArchiveResult:
    dry_run: bool
    folder: str
    archive_root: str | None
    groups: list[dict] = field(default_factory=list)


def run_archive(
    mb,
    *,
    folder: str,
    older_than: str,
    apply: bool = False,
    now: datetime | None = None,
    expect_count: int | None = None,
) -> ArchiveResult:
    """With apply, `expect_count` binds the action to the confirmed preview (#55): if the
    set changed size since then, nothing is created and nothing moves."""
    if now is None:
        now = datetime.now(UTC)
    cutoff = (now - parse_age(older_than)).date()

    archive_root = resolve_folder(mb, "archive") or "Archive"
    mb.folder.set(folder)
    msgs = list(mb.fetch(AND(date_lt=cutoff), headers_only=True, mark_seen=False, bulk=True))

    by_year: dict[int, list[str]] = defaultdict(list)
    for m in msgs:
        if not m.uid or not isinstance(m.date, datetime):
            continue
        d = m.date if m.date.tzinfo else m.date.replace(tzinfo=UTC)
        by_year[d.year].append(m.uid)

    groups: list[dict] = []
    for year in sorted(by_year):
        target = f"{archive_root}/{year}"
        uids = by_year[year]
        groups.append({"year": year, "target": target, "uids": uids, "count": len(uids)})

    if apply:
        # before the first folder is created: a refused apply leaves the mailbox as it was
        check_expected(expect_count, sum(g["count"] for g in groups))
    for g in groups:
        target, uids = g["target"], g["uids"]
        if apply and uids:
            try:
                if not mb.folder.exists(target):
                    mb.folder.create(target)
            except Exception:
                # Some servers don't have folder.exists; create and ignore "already exists"
                try:
                    mb.folder.create(target)
                except Exception:
                    pass
            move_in_batches(mb, uids, target)

    return ArchiveResult(dry_run=not apply, folder=folder, archive_root=archive_root, groups=groups)
