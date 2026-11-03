"""Retention for the bot's records (#76 L5, Dr. Sommer's condition 5), run daily by root.

- run files (stdout and transcript, one per run): 14 days, by modification time
- digests: 30 days, by modification time
- host-audit.log: 90 days, by the `timestamp` of each line

The audit log carries the system append-only flag, which only root can clear, and only at
securelevel 0. It is pruned by writing the kept lines to a new file beside it, giving that
file the old one's owner, mode and flag, clearing the old file's flag and renaming the new
one over it. That sequence is measured on the target Mac only.

"Backups included" is not done here: a pruner cannot reach a backup. The builder proposes
excluding these paths from backups, which is the owner's decision.

Standard library only, nothing imported from the package, like gate.py.
"""

from __future__ import annotations

import json
import os
import stat
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

BASE = Path("/Library/Application Support/mailbox-autopilot")
RUNS_DIR = BASE / "runs"
DIGESTS_DIR = BASE / "digests"
HOST_AUDIT = BASE / "host-audit.log"

RUN_DAYS = 14
DIGEST_DAYS = 30
AUDIT_DAYS = 90
DAY_S = 86400


def prune_dir(directory: Path, days: int, now: float) -> list[str]:
    """Remove regular files older than `days` from `directory`. Symlinks and
    subdirectories are left alone and reported, never followed."""
    removed = []
    if not directory.is_dir():
        return removed
    for entry in sorted(directory.iterdir()):
        st = entry.lstat()
        if not stat.S_ISREG(st.st_mode):
            continue
        if now - st.st_mtime > days * DAY_S:
            entry.unlink()
            removed.append(entry.name)
    return removed


def _line_time(line: bytes) -> datetime | None:
    try:
        stamp = json.loads(line)["timestamp"]
        return datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except (ValueError, KeyError, TypeError, AttributeError):
        return None


def prune_audit(path: Path, days: int, now: float, *, flags: bool = True) -> int:
    """Drop lines older than `days`. A line whose time cannot be read is kept: losing an
    audit record is worse than keeping one too long. Returns the number dropped.
    `flags=False` only in tests, which cannot set the system flag."""
    if not path.is_file():
        return 0
    cutoff = datetime.fromtimestamp(now, UTC) - timedelta(days=days)
    lines = path.read_bytes().splitlines(keepends=True)
    kept = [x for x in lines if (t := _line_time(x)) is None or t >= cutoff]
    dropped = len(lines) - len(kept)
    if dropped == 0:
        return 0
    st = path.stat()
    fresh = path.with_name(path.name + ".retention")
    fd = os.open(fresh, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, b"".join(kept))
        if os.geteuid() == 0:
            os.fchown(fd, st.st_uid, st.st_gid)
        os.fchmod(fd, stat.S_IMODE(st.st_mode))
        os.fsync(fd)
    finally:
        os.close(fd)
    if flags:
        os.chflags(fresh, st.st_flags | stat.SF_APPEND)
        os.chflags(path, st.st_flags & ~stat.SF_APPEND)
    os.replace(fresh, path)
    return dropped


def main() -> int:
    now = time.time()
    report = {
        "runs": prune_dir(RUNS_DIR, RUN_DAYS, now),
        "digests": prune_dir(DIGESTS_DIR, DIGEST_DAYS, now),
        "audit_lines_dropped": prune_audit(HOST_AUDIT, AUDIT_DAYS, now),
    }
    print(json.dumps({"retention": report}, sort_keys=True), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
