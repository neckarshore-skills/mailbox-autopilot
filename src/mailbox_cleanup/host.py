"""The mail bot's MCP host (#76 section 2): the bot's only interface to the mailbox.

It serves exactly one caller, the bot: a member of the root-created group whose name the
valid, root-owned profile carries (`profile.host_profile`). There is no unrestricted
mode, no flag and no env variable that skips that check (section 1.3). Before it serves
anything it opens its audit log, a file root provisioned at a fixed path in the anchor
directory, owned by the bot and append-only (`open_append_only`); if it cannot, it does
not serve.

Every `tools/call` takes the same path, in this order (section 5.7):

1. The tool name against the profile's allowlist. A name outside it is refused and
   audited, and its arguments are never looked at.
2. The arguments must be an object.
3. The tool runs; a guard refusal inside it is returned with its own code.
4. One audit record. A tool result leaves the host only after its record is written:
   if the record cannot be written, the caller gets a refusal instead of the data.

The audit carries no argument and no value. A tool name is recorded only if this program
knows it; anything else is "unknown", because the name is model-chosen text.

Slice 3c-1 builds the host and its refusal path; it offers no tool yet. The tools come
in the next slices, each through `build_tools`, each on this path.
"""

from __future__ import annotations

import json
import os
import secrets
import sys
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

from . import __version__, profile
from .guards import GuardRefused, check_no_symlink_component, open_append_only
from .mcp_stdio import StdioServer
from .profile import ProfileRefused

HOST_AUDIT_NAME = "host-audit.log"  # in profile.ANCHOR_DIR, provisioned by root
SERVER_NAME = "mailbox-autopilot"

# The tools the bot profile offers (section 2.2). `move` and `flag` are each a preview
# and an apply, because the apply takes no UIDs: it gets them from the preview's token.
ALLOWED_TOOLS = frozenset(
    {
        "create_folder",
        "draft",
        "flag_apply",
        "flag_preview",
        "move_apply",
        "move_preview",
        "read",
        "search",
        "thread",
    }
)
# What the profile excludes (section 2.3), by the CLI's names. Refused like any unknown
# name, but recorded under its own name: a burst of `delete` is what an injection looks
# like from the outside (section 5.4).
EXCLUDED_TOOLS = frozenset(
    {
        "archive",
        "attachments",
        "auth",
        "bounces",
        "compose",
        "config",
        "dedupe",
        "delete",
        "outbox",
        "playbook",
        "playbooks",
        "save-attachment",
        "scan",
        "senders",
        "unsubscribe",
    }
)
KNOWN_NAMES = ALLOWED_TOOLS | EXCLUDED_TOOLS


def _refusal(rule: str, message: str) -> GuardRefused:
    return GuardRefused("profile_refused", rule, message)


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view) :]


class HostAudit:
    """The host's audit log: one JSON line per event, appended to a root-provisioned
    file the bot owns but cannot truncate or delete (#76 section 5.5, D15)."""

    def __init__(self, path: Path, *, run_id: str, caller_uid: int, profile_sha256):
        self.path = Path(path)
        self._base = {
            "run_id": run_id,
            "caller_uid": caller_uid,
            "profile": profile_sha256 is not None,
            "profile_sha256": profile_sha256,
        }

    def _open(self) -> int:
        check_no_symlink_component(self.path, prefix="audit_log", what="the host audit log")
        return open_append_only(self.path, prefix="audit_log", what="the host audit log")

    def check(self) -> None:
        """Raise GuardRefused unless the log can be appended to now."""
        os.close(self._open())

    def write(self, **fields) -> None:
        record = {"timestamp": _now(), **self._base, **fields}
        line = json.dumps(record, ensure_ascii=True, sort_keys=True) + "\n"
        fd = self._open()
        try:
            _write_all(fd, line.encode("ascii"))
        finally:
            os.close(fd)


def _result(payload: dict, *, error: bool) -> dict:
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return {"content": [{"type": "text", "text": text}], "isError": error}


def _error_result(e: GuardRefused) -> dict:
    return _result(
        {"error_code": e.error_code, "rule": e.rule, "exit_code": e.exit_code, "message": str(e)},
        error=True,
    )


class Host:
    def __init__(self, *, profile, audit: HostAudit, tools: Mapping):
        outside = set(tools) - ALLOWED_TOOLS
        if outside:
            raise ValueError(f"tools outside the bot allowlist: {sorted(outside)}")
        self.profile = profile
        self.audit = audit
        self.tools = dict(tools)

    def list_tools(self) -> list[dict]:
        return [self.tools[n].spec for n in sorted(self.tools)]

    def _record(self, name, **fields) -> None:
        known = name if type(name) is str and name in KNOWN_NAMES else "unknown"
        self.audit.write(event="call", tool=known, **fields)

    def _refused(self, name, e: GuardRefused) -> dict:
        try:
            self._record(name, result="refused", **e.audit_fields())
        except GuardRefused:
            pass  # the refusal stands without its line; the host never acts on it
        return _error_result(e)

    def call(self, name, arguments) -> dict:
        tool = self.tools.get(name) if type(name) is str else None
        if tool is None:
            return self._refused(
                name, _refusal("tool_not_allowed", "the bot profile does not offer this tool")
            )
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, dict):
            return self._refused(name, _refusal("arguments_invalid", "arguments must be an object"))
        try:
            payload = tool.run(arguments)
        except GuardRefused as e:
            return self._refused(name, e)
        except Exception as e:
            try:
                self._record(
                    name, result="error", error_code="operation_error", rule=type(e).__name__
                )
            except GuardRefused:
                pass
            return _result(
                {"error_code": "operation_error", "message": "the operation failed"}, error=True
            )
        try:
            self._record(name, result="ok")
        except GuardRefused as e:
            return _error_result(e)  # no record, no data
        return _result(payload, error=False)


def build_tools(p) -> dict:
    """The tools for this profile. None yet: slice 3c-1 builds the host only."""
    return {}


def _say(message: str) -> None:
    sys.stderr.write(f"mailbox-autopilot-host: {message}\n")
    sys.stderr.flush()


def run(inp, out, *, uid: int) -> int:
    """Serve one session on `inp`/`out`, or refuse. Returns the process exit code."""
    run_id = secrets.token_hex(8)
    log = profile.ANCHOR_DIR / HOST_AUDIT_NAME
    try:
        p = profile.host_profile(uid)
    except ProfileRefused as e:
        _say(f"refused: {e.rule}")
        try:
            HostAudit(log, run_id=run_id, caller_uid=uid, profile_sha256=None).write(
                event="start", result="refused", error_code="profile_refused", rule=e.rule
            )
        except GuardRefused:
            pass  # nothing installed to write to; stderr carries the reason
        return profile.REFUSED_EXIT
    audit = HostAudit(log, run_id=run_id, caller_uid=uid, profile_sha256=p.sha256)
    try:
        audit.check()
        h = Host(profile=p, audit=audit, tools=build_tools(p))
        audit.write(event="start", result="ok")
    except GuardRefused as e:
        _say(f"refused: {e.rule}")
        return profile.REFUSED_EXIT
    StdioServer(name=SERVER_NAME, version=__version__, tools=h.list_tools, call=h.call).serve(
        inp, out
    )
    try:
        audit.write(event="stop", result="ok")
    except GuardRefused:
        pass
    return 0


def main() -> int:
    """Entry point. File descriptor 1 is the protocol stream, so it is taken over first:
    the frames go to a private copy, and anything else that writes to fd 1 or
    sys.stdout (a stray print, a library) lands on stderr instead."""
    frames = os.fdopen(os.dup(1), "wb")
    os.dup2(2, 1)
    sys.stdout = sys.stderr
    try:
        return run(sys.stdin.buffer, frames, uid=os.getuid())
    finally:
        frames.close()


if __name__ == "__main__":
    sys.exit(main())
