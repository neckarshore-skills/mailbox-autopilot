"""Audit log writer. One JSON object per line in ~/.mailbox-cleanup/audit.log.

Since 0.8.0 (#75): the file and its rotated copies are owner-only (0600), the default
directory is 0700, the live file rotates at MAX_BYTES and BACKUP_COUNT copies are kept,
and no record carries an argument VALUE — only the names of the arguments given.
"""

import json
import os
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path

AUDIT_LOG_PATH_ENV = "MAILBOX_CLEANUP_AUDIT_LOG"
DEFAULT_AUDIT_LOG = Path.home() / ".mailbox-cleanup" / "audit.log"

FILE_MODE = 0o600
DIR_MODE = 0o700
# Generous on purpose: the log is the only record of what a cleanup moved, so rotation
# should drop history only after a long time of normal use.
MAX_BYTES = 5 * 1024 * 1024
BACKUP_COUNT = 5


def _audit_path() -> Path:
    override = os.environ.get(AUDIT_LOG_PATH_ENV)
    return Path(override) if override else DEFAULT_AUDIT_LOG


def _is_default_location() -> bool:
    """Only the tool's own directory is tightened. The parent of an override path may be
    a directory the tool does not own (a home folder, /tmp), so it is left alone."""
    return not os.environ.get(AUDIT_LOG_PATH_ENV)


def _log_files(path: Path) -> list[Path]:
    return [path, *(path.with_name(f"{path.name}.{i}") for i in range(1, BACKUP_COUNT + 1))]


def repair_permissions() -> None:
    """Tighten an existing log, its rotated copies and the default directory.

    Runs at every CLI start, so a log created by an older version is fixed by the next
    run of any command, including a dry run that writes nothing. Creates nothing.
    """
    path = _audit_path()
    if _is_default_location() and path.parent.is_dir():
        os.chmod(path.parent, DIR_MODE)
    for p in _log_files(path):
        if p.is_file():
            os.chmod(p, FILE_MODE)


def _rotate_if_needed(path: Path) -> None:
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        return
    if size < MAX_BYTES:
        return
    # audit.log.N-1 -> audit.log.N, ..., audit.log -> audit.log.1; the oldest is replaced.
    for i in range(BACKUP_COUNT - 1, 0, -1):
        src = path.with_name(f"{path.name}.{i}")
        if src.exists():
            os.replace(src, path.with_name(f"{path.name}.{i + 1}"))
    os.replace(path, path.with_name(f"{path.name}.1"))


def _append(record: Mapping[str, object], path: Path | None = None) -> None:
    """`path` set: the tool's own directory under a home from the user database (#76),
    tightened like the default one. Unset: the default location or the env override."""
    own_dir = path is not None or _is_default_location()
    path = path if path is not None else _audit_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if own_dir:
        os.chmod(path.parent, DIR_MODE)  # mkdir's mode is filtered by the umask
    _rotate_if_needed(path)
    line = json.dumps(record, ensure_ascii=False) + "\n"
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, FILE_MODE)
    try:
        os.fchmod(fd, FILE_MODE)  # O_CREAT's mode does not touch an existing file
        os.write(fd, line.encode("utf-8"))
    finally:
        os.close(fd)


def _require_str_result(result: object) -> None:
    if not isinstance(result, str):
        raise TypeError(f"audit result must be a string, got {type(result).__name__}")


def _given(value: object) -> bool:
    return value is not None and value is not False and value != "" and value != [] and value != ()


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def log_action(
    *,
    subcommand: str,
    account: str,
    args: Mapping[str, object],
    folder: str,
    affected_uids: Sequence[str],
    result: str,
    error: str | None = None,
    target_folder: str | None = None,
) -> None:
    """Append one JSON-line record describing an applied action.

    `account` is the alias of the account the action was performed against.
    `args` is reduced here, at the sink, to the sorted names of the arguments that were
    given (`arg_keys`): filter values are addresses and subjects (#75). The account's
    keep list is recorded as its length only (`keep_config_count`), which still says
    whether a list was in force when a mail survived. `target_folder` (move) is kept
    as a value: it is where the mail went, not what selected it.
    `result` is always a string ("success", "partial_failure", "error", and for
    `unsubscribe` also "manual", "failed" and "kept"); readers
    compare it as one, so a structured value would read as an unknown outcome (#51).
    """
    _require_str_result(result)
    record: dict[str, object] = {
        "timestamp": _now(),
        "subcommand": subcommand,
        "account": account,
        "arg_keys": sorted(k for k, v in args.items() if k != "keep_config" and _given(v)),
        "folder": folder,
        "affected_uids": list(affected_uids),
        "result": result,
    }
    if target_folder is not None:
        record["target_folder"] = target_folder
    if "keep_config" in args:
        record["keep_config_count"] = len(args["keep_config"] or ())
    if error is not None:
        record["error"] = error
    _append(record)


def log_manage_action(
    *,
    subcommand: str,
    account: str,
    folder: str,
    uids: Sequence[str],
    result: str,
    arg_keys: Iterable[str] = (),
    error: str | None = None,
) -> None:
    """Manage-layer record (spec §5): argument KEYS only, never values, because
    search values are names and topics."""
    _require_str_result(result)
    record: dict[str, object] = {
        "timestamp": _now(),
        "subcommand": subcommand,
        "account": account,
        "arg_keys": sorted(arg_keys),
        "folder": folder,
        "affected_uids": list(uids),
        "result": result,
    }
    if error is not None:
        record["error"] = error
    _append(record)


def log_refusal(
    *, home: Path, command: str, rule: str, caller_uid: int, profile_sha256: str | None
) -> None:
    """One record for a refusal by the bot profile (#76), before any argument was parsed.

    Written to `<home>/.mailbox-cleanup/audit.log`, where the caller passes the home from
    the user database: neither HOME nor MAILBOX_CLEANUP_AUDIT_LOG can move or silence it.
    `command` is the known command path ("delete", "manage search") or "unknown"; no
    argument and no value is recorded.
    """
    _append(
        {
            "timestamp": _now(),
            "subcommand": command,
            "result": "refused",
            "error_code": "profile_refused",
            "rule": rule,
            "caller_uid": caller_uid,
            "profile_sha256": profile_sha256,
        },
        path=home / ".mailbox-cleanup" / "audit.log",
    )
