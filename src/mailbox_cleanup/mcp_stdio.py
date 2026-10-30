"""The MCP stdio transport for the bot host (#76 section 2, slice 3c-1).

Hand-rolled on purpose. The host holds the IMAP credential, so no third-party protocol
stack runs next to it (section 1.5), and no dependency is added that would need its own
exact pins (release condition 3). This module knows JSON-RPC 2.0 framed one message per
line, and the five MCP methods the host needs: `initialize`, `ping`, `tools/list`,
`tools/call` and the `notifications/*` it may ignore. It knows nothing about mail,
profiles or guards.

`tools/call` hands the tool name and arguments to the host exactly as they arrived,
unvalidated: the host's profile check runs before any argument is looked at (section
5.7). The host's `call` is expected never to raise; if it does, the client gets an
internal error with no detail, because an exception message can carry a folder name.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import BinaryIO

# Versions this server answers with. The handshake echoes a requested version from this
# list and otherwise answers with the last one; the client decides whether to go on.
# The subset used here (tools/list, tools/call with text content and isError) is the same
# in all three.
PROTOCOL_VERSIONS = ("2024-11-05", "2025-03-26", "2025-06-18")
MAX_LINE = 1024 * 1024  # one request; the host's tools take short parameters only

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603


class _Invalid(Exception):
    def __init__(self, code: int, message: str, id=None):
        super().__init__(message)
        self.code = code
        self.id = id


def _no_constants(name: str):
    raise ValueError(f"{name} is not JSON")  # NaN, Infinity, -Infinity


def _frame(obj: dict) -> bytes:
    # ensure_ascii: every non-ASCII character, U+2028 included, travels escaped, so a
    # frame is one line whatever a tool returns
    return json.dumps(obj, ensure_ascii=True, separators=(",", ":")).encode("ascii") + b"\n"


def _error(id, code: int, message: str) -> bytes:
    return _frame({"jsonrpc": "2.0", "id": id, "error": {"code": code, "message": message}})


def _valid_id(value) -> bool:
    return type(value) is str or type(value) is int  # not bool, not float, not null


class StdioServer:
    def __init__(
        self,
        *,
        name: str,
        version: str,
        tools: Callable[[], list[dict]],
        call: Callable[[object, object], dict],
    ):
        self._info = {"name": name, "version": version}
        self._tools = tools
        self._call = call
        self._initialized = False

    # one message ---------------------------------------------------------------------

    def handle_line(self, line: bytes) -> bytes | None:
        """One request in, one framed answer out; None for a notification or a blank."""
        if not line.strip():
            return None
        try:
            msg = json.loads(line.decode("utf-8"), parse_constant=_no_constants)
        except (UnicodeDecodeError, ValueError):
            return _error(None, PARSE_ERROR, "parse error")
        try:
            return self._dispatch(msg)
        except _Invalid as e:
            return _error(e.id, e.code, str(e))

    def _dispatch(self, msg) -> bytes | None:
        if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
            raise _Invalid(INVALID_REQUEST, "invalid request")
        if "method" not in msg:
            return None  # a response from the client: this server sends no requests
        has_id = "id" in msg
        id = msg.get("id")
        if has_id and not _valid_id(id):
            raise _Invalid(INVALID_REQUEST, "invalid request id")
        method = msg["method"]
        if type(method) is not str:
            raise _Invalid(INVALID_REQUEST, "method must be a string", id)
        if not has_id:
            return None  # notifications/initialized, notifications/cancelled, anything
        params = msg.get("params")
        result = self._method(method, params, id)
        return _frame({"jsonrpc": "2.0", "id": id, "result": result})

    def _method(self, method: str, params, id) -> dict:
        if method == "ping":
            return {}
        if method == "initialize":
            if self._initialized:
                raise _Invalid(INVALID_REQUEST, "already initialized", id)
            requested = params.get("protocolVersion") if isinstance(params, dict) else None
            version = requested if requested in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[-1]
            self._initialized = True
            return {
                "protocolVersion": version,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": dict(self._info),
            }
        if method in ("tools/list", "tools/call") and not self._initialized:
            raise _Invalid(INVALID_REQUEST, "not initialized", id)
        if method == "tools/list":
            return {"tools": self._tools()}
        if method == "tools/call":
            if not isinstance(params, dict):
                raise _Invalid(INVALID_PARAMS, "params must be an object", id)
            try:
                return self._call(params.get("name"), params.get("arguments"))
            except Exception:
                raise _Invalid(INTERNAL_ERROR, "internal error", id) from None
        raise _Invalid(METHOD_NOT_FOUND, "method not found", id)

    # the stream ----------------------------------------------------------------------

    def serve(self, inp: BinaryIO, out: BinaryIO) -> None:
        """Read until end of input; answer each line in order and flush each answer."""
        while True:
            line = inp.readline(MAX_LINE + 1)
            if not line:
                return
            if len(line) > MAX_LINE and not line.endswith(b"\n"):
                while (rest := inp.readline(MAX_LINE + 1)) and not rest.endswith(b"\n"):
                    pass  # drop the remainder of the oversized line
                answer = _error(None, INVALID_REQUEST, "request too large")
            else:
                answer = self.handle_line(line)
            if answer is not None:
                out.write(answer)
                out.flush()
