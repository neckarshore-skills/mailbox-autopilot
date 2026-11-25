"""Retention for the bot's records (#76 L5, Dr. Sommer's condition 5), run daily by root.

- run files (stdout and transcript, one per run): 14 days, by modification time
- digests: 30 days, by modification time
- host-audit.log: 90 days, by the `timestamp` of each line
- launcher.stderr and retention.stderr (L13): moved aside once a day when not empty, and
  the moved files kept 14 days, like the run files, since claude's own stderr lands there
- first of all, whatever the bot user still runs is killed (C3), like gate.py does

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
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

BASE = Path("/Library/Application Support/mailbox-autopilot")
RUNS_DIR = BASE / "runs"
DIGESTS_DIR = BASE / "digests"
HOST_AUDIT = BASE / "host-audit.log"
STDERR_FILES = (BASE / "launcher.stderr", BASE / "retention.stderr")  # plists.py, L13
STDERR_DIR = BASE / "stderr"
BOT_USER = "mailbox-autopilot-bot"  # gate.py's; a test holds the two equal

RUN_DAYS = 14
DIGEST_DAYS = 30
AUDIT_DAYS = 90
DAY_S = 86400
SF_APPEND = getattr(stat, "SF_APPEND", 0x00040000)  # the system append-only flag


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


def rotate_stderr(paths, dest: Path, now: float) -> list[str]:
    """Move each filled stderr file into `dest` under a dated name. launchd opens the path
    afresh each time it starts the job, and this job runs outside the run hours, so the
    next run writes to what is in the path then. An empty file stays; a symlink is never
    touched.

    C6: launchd creates a missing StandardErrorPath file 0644, whatever the plist's Umask
    says, and appends to an existing one without changing its mode (measured 2026-11-25,
    macOS 27.2.1, gui domain). So a moved or absent file is replaced at once by an empty
    0600 one, and launchd appends to that."""
    dest.mkdir(mode=0o700, exist_ok=True)
    stamp = datetime.fromtimestamp(now, UTC).strftime("%Y%m%dT%H%M%SZ")
    moved = []
    for path in paths:
        try:
            st = path.lstat()
        except FileNotFoundError:
            _owner_only_empty(path)
            continue
        if not stat.S_ISREG(st.st_mode) or st.st_size == 0:
            continue
        name = f"{path.name}-{stamp}"
        os.replace(path, dest / name)
        moved.append(name)
        _owner_only_empty(path)
    return moved


def _owner_only_empty(path: Path) -> None:
    """An empty 0600 file in `path`; never follows or replaces what is already there."""
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        return
    os.fchmod(fd, 0o600)  # the mode argument is masked by the umask; this is not
    os.close(fd)


def _line_time(line: bytes) -> datetime | None:
    """The line's time, or None when it cannot be read; a time without a zone cannot be
    compared with the cutoff, so it counts as unreadable and the line is kept."""
    try:
        stamp = json.loads(line)["timestamp"]
        when = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except (ValueError, KeyError, TypeError, AttributeError):
        return None
    return when if when.tzinfo is not None else None


def prune_audit(path: Path, days: int, now: float, *, flag: int | None = SF_APPEND) -> int:
    """Drop lines older than `days`. A line whose time cannot be read is kept: losing an
    audit record is worse than keeping one too long. Returns the number dropped.

    macOS refuses to rename a file that carries an append-only flag, and refuses to
    replace one (measured with `uappend`). So the order is: write the kept lines to a
    fresh file with no flag, clear the live file's flag, replace, then set the flag on
    the final path. Between the clear and the set the host refuses the log, which fails
    closed, and the job runs outside the run hours. A leftover fresh file from a run that
    died is removed first, and the flag is set on the live log on every run, so a run
    that died after the clear is healed by the next one.

    `flag` is SF_APPEND in production. Tests pass UF_APPEND, which an owner may set and
    clear, or None where chflags does not exist."""
    if not path.is_file():
        return 0
    fresh = path.with_name(path.name + ".retention")
    if fresh.is_symlink() or fresh.exists():
        fresh.unlink()
    cutoff = datetime.fromtimestamp(now, UTC) - timedelta(days=days)
    lines = path.read_bytes().splitlines(keepends=True)
    kept = [x for x in lines if (t := _line_time(x)) is None or t >= cutoff]
    dropped = len(lines) - len(kept)
    if dropped:
        st = path.stat()
        fd = os.open(fresh, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            _write_all(fd, b"".join(kept))
            if os.geteuid() == 0:
                os.fchown(fd, st.st_uid, st.st_gid)
            os.fchmod(fd, stat.S_IMODE(st.st_mode))
            os.fsync(fd)
        finally:
            os.close(fd)
        if flag is not None:
            os.chflags(path, st.st_flags & ~flag)
        try:
            os.replace(fresh, path)
        except OSError:
            # C7: the live log keeps its lines; give it its flag back before reporting.
            if flag is not None:
                os.chflags(path, st.st_flags | flag)
            raise
    if flag is not None:
        os.chflags(path, path.stat().st_flags | flag)
    return dropped


def kill_leftovers(user: str = BOT_USER) -> int:
    """C3 (James, #101): gate.py kills what the bot user still runs at the start of each
    run, but the last run is at 20:00 and the next at 07:00, so an orphan of the evening
    run would live eleven hours. This job at 05:30 kills it too."""
    return subprocess.run(["/usr/bin/pkill", "-KILL", "-U", user], check=False).returncode


def _write_all(fd: int, data: bytes) -> None:
    """C7: os.write may write less than it was given; a short write would drop records."""
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view) :]


def main() -> int:
    now = time.time()
    report = {
        "leftovers_killed": kill_leftovers() == 0,
        "runs": prune_dir(RUNS_DIR, RUN_DAYS, now),
        "digests": prune_dir(DIGESTS_DIR, DIGEST_DAYS, now),
        "audit_lines_dropped": prune_audit(HOST_AUDIT, AUDIT_DAYS, now),
        "stderr_rotated": rotate_stderr(STDERR_FILES, STDERR_DIR, now),
        "stderr_pruned": prune_dir(STDERR_DIR, RUN_DAYS, now),
    }
    print(json.dumps({"retention": report}, sort_keys=True), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
