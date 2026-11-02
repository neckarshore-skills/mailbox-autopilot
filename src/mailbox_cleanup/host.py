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

Release 1 is a read-only bot (owner decision 2026-11-02, #76): the host offers `search`,
`read` and `thread`, built in `host_tools`, and no write tool can be installed.

A session that collects REFUSAL_BUDGET refusals ends: a burst of refused calls is what an
injection looks like from the outside (section 5.4), and the host acts on it.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import sys
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

from . import __version__, host_tools, profile
from .guards import GuardRefused, check_no_symlink_component, open_append_only
from .mcp_stdio import StdioServer
from .profile import ProfileRefused

HOST_AUDIT_NAME = "host-audit.log"  # in profile.ANCHOR_DIR, provisioned by root
SERVER_NAME = "mailbox-autopilot"

# What release 1 offers (owner decision 2026-11-02). Acceptance 5b pins this list.
ALLOWED_TOOLS = frozenset({"read", "search", "thread"})
# What the profile excludes (section 2.3), by the CLI's names, and the writes of the later
# stage. Refused like any unknown name, but recorded under its own name: a burst of
# `delete` is what an injection looks like from the outside (section 5.4).
EXCLUDED_TOOLS = frozenset(
    {
        "archive",
        "attachments",
        "auth",
        "bounces",
        "compose",
        "config",
        "create-folder",
        "dedupe",
        "delete",
        "draft",
        "flag",
        "flag-apply",
        "flag-preview",
        "move",
        "move-apply",
        "move-preview",
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
REFUSAL_BUDGET = 20  # James on #94, finding 5; a well-behaved run stays far below it
# What a record may say. `rule`, `action`, `result` and `error_code` are program constants
# by contract; this holds the contract, so one formatted rule cannot put a folder name
# into a log that promises to carry no value (James on #94, finding 2).
_TOKEN_RE = re.compile(r"[a-z0-9_]{1,64}", re.ASCII)
_CLASS_RE = re.compile(r"[A-Za-z0-9_]{1,64}", re.ASCII)


def _shaped(value, shape: re.Pattern) -> str:
    return value if type(value) is str and shape.fullmatch(value) else "unknown"


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
    text = json.dumps(
        payload, ensure_ascii=False, allow_nan=False, separators=(",", ":"), sort_keys=True
    )
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
        self.refusals = 0

    @property
    def exhausted(self) -> bool:
        return self.refusals >= REFUSAL_BUDGET

    def list_tools(self) -> list[dict]:
        return [self.tools[n].spec for n in sorted(self.tools)]

    def _record(self, name, *, exception=None, count=None, **tokens) -> None:
        known = name if type(name) is str and name in KNOWN_NAMES else "unknown"
        fields = {k: _shaped(v, _TOKEN_RE) for k, v in tokens.items()}
        if exception is not None:
            fields["exception"] = _shaped(exception, _CLASS_RE)
        if type(count) is int:
            fields["count"] = count
        self.audit.write(event="call", tool=known, **fields)

    def _refused(self, name, e: GuardRefused) -> dict:
        self.refusals += 1
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
            # built before the record: an ok line means a result exists (James on #94)
            payload = tool.run(arguments)
            result = _result(payload, error=False)
        except GuardRefused as e:
            return self._refused(name, e)
        except Exception as e:
            try:
                self._record(
                    name,
                    result="error",
                    error_code="operation_error",
                    exception=type(e).__name__,
                )
            except GuardRefused:
                pass
            return _result(
                {"error_code": "operation_error", "message": "the operation failed"}, error=True
            )
        try:
            self._record(name, result="ok", count=payload.get("count"))
        except GuardRefused as e:
            return _error_result(e)  # no record, no data
        return result


def build_tools(p) -> dict:
    return host_tools.build(p)


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
    server = StdioServer(
        name=SERVER_NAME,
        version=__version__,
        tools=h.list_tools,
        call=h.call,
        stop=lambda: h.exhausted,
    )
    server.serve(inp, out)
    stopped = {"result": "ok"}
    if h.exhausted:
        stopped = {"result": "refused", "error_code": "profile_refused", "rule": "refusal_budget"}
    try:
        audit.write(event="stop", **stopped)
    except GuardRefused:
        pass
    return profile.REFUSED_EXIT if h.exhausted else 0


def main() -> int:
    """Entry point. File descriptor 1 is the protocol stream, so it is taken over first:
    the frames go to a private copy, and fd 1 becomes a copy of stderr, so anything else
    that writes to it (a stray print, sys.stdout, a library, a child) lands on stderr."""
    frames = os.fdopen(os.dup(1), "wb")
    os.dup2(2, 1)
    try:
        return run(sys.stdin.buffer, frames, uid=os.getuid())
    finally:
        frames.close()


if __name__ == "__main__":
    sys.exit(main())
