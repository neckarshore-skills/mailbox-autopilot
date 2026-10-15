"""Audit log writer. One JSON object per line in ~/.mailbox-cleanup/audit.log."""

import json
import os
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path

AUDIT_LOG_PATH_ENV = "MAILBOX_CLEANUP_AUDIT_LOG"
DEFAULT_AUDIT_LOG = Path.home() / ".mailbox-cleanup" / "audit.log"


def _audit_path() -> Path:
    override = os.environ.get(AUDIT_LOG_PATH_ENV)
    return Path(override) if override else DEFAULT_AUDIT_LOG


def _require_str_result(result: object) -> None:
    if not isinstance(result, str):
        raise TypeError(f"audit result must be a string, got {type(result).__name__}")


def log_action(
    *,
    subcommand: str,
    account: str,
    args: Mapping[str, object],
    folder: str,
    affected_uids: Sequence[str],
    result: str,
    error: str | None = None,
) -> None:
    """Append one JSON-line record describing an applied action.

    `account` is the alias of the account the action was performed against.
    `result` is always a string ("success", "partial_failure", "error", and for
    `unsubscribe` also "manual" and "failed"); readers
    compare it as one, so a structured value would read as an unknown outcome (#51).
    """
    _require_str_result(result)
    record: dict[str, object] = {
        "timestamp": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "subcommand": subcommand,
        "account": account,
        "args": dict(args),
        "folder": folder,
        "affected_uids": list(affected_uids),
        "result": result,
    }
    if error is not None:
        record["error"] = error
    path = _audit_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


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
        "timestamp": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "subcommand": subcommand,
        "account": account,
        "arg_keys": sorted(arg_keys),
        "folder": folder,
        "affected_uids": list(uids),
        "result": result,
    }
    if error is not None:
        record["error"] = error
    path = _audit_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
