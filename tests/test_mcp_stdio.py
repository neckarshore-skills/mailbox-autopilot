"""#76 slice 3c-1: the MCP stdio protocol layer, hand-rolled on purpose.

The host holds the IMAP credential, so it runs no third-party protocol stack (section
1.5) and adds no dependency that would need its own exact pins (release condition 3).
This layer knows JSON-RPC and the five MCP methods the host needs. It knows nothing
about mail, profiles or guards: it hands the tool name and arguments to the host as
they arrived, unvalidated, because the host's profile check runs first (section 5.7).
"""

import io
import json

import pytest

from mailbox_cleanup import mcp_stdio as m

TOOLS = [{"name": "t", "description": "d", "inputSchema": {"type": "object"}}]


class Recorder:
    def __init__(self, result=None, raises=None):
        self.calls = []
        self.result = result or {"content": [{"type": "text", "text": "ok"}], "isError": False}
        self.raises = raises

    def __call__(self, name, arguments):
        self.calls.append((name, arguments))
        if self.raises:
            raise self.raises
        return self.result


def _server(call=None):
    return m.StdioServer(
        name="mailbox-autopilot", version="9.9.9", tools=lambda: TOOLS, call=call or Recorder()
    )


def _send(server, obj):
    raw = obj if isinstance(obj, bytes) else json.dumps(obj).encode()
    out = server.handle_line(raw)
    return None if out is None else json.loads(out)


def _init(server, version="2025-06-18"):
    r = _send(
        server,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": version,
                "capabilities": {},
                "clientInfo": {"name": "c", "version": "1"},
            },
        },
    )
    _send(server, {"jsonrpc": "2.0", "method": "notifications/initialized"})
    return r


def _call(server, name="t", arguments=None, id=7):
    params = {"name": name}
    if arguments is not None:
        params["arguments"] = arguments
    return _send(server, {"jsonrpc": "2.0", "id": id, "method": "tools/call", "params": params})


# --- handshake -----------------------------------------------------------------------


@pytest.mark.parametrize("version", m.PROTOCOL_VERSIONS)
def test_initialize_echoes_a_supported_version(version):
    r = _init(_server(), version)
    assert r["id"] == 1 and r["result"]["protocolVersion"] == version
    assert r["result"]["capabilities"] == {"tools": {"listChanged": False}}
    assert r["result"]["serverInfo"] == {"name": "mailbox-autopilot", "version": "9.9.9"}


def test_initialize_answers_an_unknown_version_with_the_latest_supported():
    r = _init(_server(), "1999-01-01")
    assert r["result"]["protocolVersion"] == m.PROTOCOL_VERSIONS[-1]


def test_a_second_initialize_is_refused():
    s = _server()
    _init(s)
    assert _init(s)["error"]["code"] == m.INVALID_REQUEST


def test_tools_are_refused_before_initialize():
    s = _server()
    r = _send(s, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    assert r["error"]["code"] == m.INVALID_REQUEST
    assert _call(s)["error"]["code"] == m.INVALID_REQUEST


def test_ping_works_at_any_time():
    s = _server()
    assert _send(s, {"jsonrpc": "2.0", "id": "p", "method": "ping"}) == {
        "jsonrpc": "2.0",
        "id": "p",
        "result": {},
    }


# --- tools ---------------------------------------------------------------------------


def test_tools_list_returns_the_hosts_tools():
    s = _server()
    _init(s)
    r = _send(s, {"jsonrpc": "2.0", "id": 3, "method": "tools/list", "params": {}})
    assert r == {"jsonrpc": "2.0", "id": 3, "result": {"tools": TOOLS}}


def test_tools_call_hands_name_and_arguments_over_unvalidated():
    """The host checks the name against the profile before anything else looks at the
    arguments (section 5.7), so this layer passes both on exactly as they arrived."""
    rec = Recorder()
    s = _server(rec)
    _init(s)
    r = _call(s, "delete", {"--anything": [1, 2]})
    assert rec.calls == [("delete", {"--anything": [1, 2]})]
    assert r == {"jsonrpc": "2.0", "id": 7, "result": rec.result}
    _call(s, 42, "garbage")
    _call(s, "t")
    assert rec.calls[1:] == [(42, "garbage"), ("t", None)]


def test_tools_call_without_params_object_is_invalid():
    rec = Recorder()
    s = _server(rec)
    _init(s)
    r = _send(s, {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": ["t"]})
    assert r["error"]["code"] == m.INVALID_PARAMS and rec.calls == []


def test_an_exception_in_the_host_is_an_internal_error_without_detail():
    s = _server(Recorder(raises=RuntimeError("secret folder name")))
    _init(s)
    r = _call(s)
    assert r["error"]["code"] == m.INTERNAL_ERROR
    assert "secret" not in json.dumps(r)


# --- JSON-RPC shape ------------------------------------------------------------------


def test_an_unknown_method_is_not_found():
    s = _server()
    _init(s)
    r = _send(s, {"jsonrpc": "2.0", "id": 5, "method": "resources/list"})
    assert r["error"]["code"] == m.METHOD_NOT_FOUND


@pytest.mark.parametrize("raw", [b"{not json", b'{"x": NaN}', b'{"x": Infinity}', b"\xff\xfe"])
def test_unparseable_input_is_a_parse_error_with_a_null_id(raw):
    r = _send(_server(), raw)
    assert r["error"]["code"] == m.PARSE_ERROR and r["id"] is None


@pytest.mark.parametrize(
    "obj",
    [
        [{"jsonrpc": "2.0", "id": 1, "method": "ping"}],  # batches are not part of MCP
        "ping",
        {"jsonrpc": "1.0", "id": 1, "method": "ping"},
        {"id": 1, "method": "ping"},
        {"jsonrpc": "2.0", "id": 1, "method": 5},
        {"jsonrpc": "2.0", "id": True, "method": "ping"},
        {"jsonrpc": "2.0", "id": 1.5, "method": "ping"},
        {"jsonrpc": "2.0", "id": None, "method": "ping"},
        {"jsonrpc": "2.0", "id": [1], "method": "ping"},
    ],
    ids=[
        "batch",
        "string",
        "v1",
        "no-version",
        "method-not-str",
        "bool-id",
        "float-id",
        "null-id",
        "list-id",
    ],
)
def test_an_invalid_request_is_refused(obj):
    r = _send(_server(), obj)
    assert r["error"]["code"] == m.INVALID_REQUEST


def test_a_valid_id_survives_into_an_invalid_request_error():
    r = _send(_server(), {"jsonrpc": "2.0", "id": 9, "method": 5})
    assert r["id"] == 9


def test_notifications_are_never_answered():
    s = _server()
    for method in ("notifications/initialized", "notifications/cancelled", "tools/call", "unknown"):
        assert _send(s, {"jsonrpc": "2.0", "method": method}) is None


def test_a_notification_does_not_call_a_tool():
    rec = Recorder()
    s = _server(rec)
    _init(s)
    _send(s, {"jsonrpc": "2.0", "method": "tools/call", "params": {"name": "t"}})
    assert rec.calls == []


def test_a_response_from_the_client_is_ignored():
    assert _send(_server(), {"jsonrpc": "2.0", "id": 1, "result": {}}) is None


def test_every_output_is_one_line():
    s = _server(
        Recorder(result={"content": [{"type": "text", "text": "a\nb c"}], "isError": False})
    )
    _init(s)
    out = s.handle_line(
        json.dumps(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "t"}}
        ).encode()
    )
    assert out.endswith(b"\n") and out.count(b"\n") == 1 and out.isascii()


# --- the stream ----------------------------------------------------------------------


def _lines(*objs):
    return b"".join(json.dumps(o).encode() + b"\n" for o in objs)


def test_serve_answers_line_by_line_and_skips_blank_lines():
    s = _server()
    inp = io.BytesIO(
        _lines({"jsonrpc": "2.0", "id": 1, "method": "ping"})
        + b"\n   \n"
        + _lines({"jsonrpc": "2.0", "id": 2, "method": "ping"})
    )
    out = io.BytesIO()
    s.serve(inp, out)
    assert [json.loads(x)["id"] for x in out.getvalue().splitlines()] == [1, 2]


def test_an_oversized_line_is_refused_and_the_stream_goes_on(monkeypatch):
    monkeypatch.setattr(m, "MAX_LINE", 100)
    s = _server()
    inp = io.BytesIO(b"x" * 250 + b"\n" + _lines({"jsonrpc": "2.0", "id": 2, "method": "ping"}))
    out = io.BytesIO()
    s.serve(inp, out)
    first, second = (json.loads(x) for x in out.getvalue().splitlines())
    assert first["error"]["code"] == m.INVALID_REQUEST and first["id"] is None
    assert second == {"jsonrpc": "2.0", "id": 2, "result": {}}


def test_serve_flushes_after_every_answer():
    class Out(io.BytesIO):
        flushes = 0

        def flush(self):
            Out.flushes += 1

    out = Out()
    _server().serve(io.BytesIO(_lines({"jsonrpc": "2.0", "id": 1, "method": "ping"})), out)
    assert Out.flushes >= 1


def test_serve_ends_at_end_of_input():
    out = io.BytesIO()
    _server().serve(io.BytesIO(b""), out)
    assert out.getvalue() == b""


def test_deep_nesting_is_a_parse_error_not_a_crash():
    """json.loads raises RecursionError, not ValueError, on deep nesting. The arguments of
    a tools/call are model-chosen, so a crash here would end the host (security review
    of f63c581)."""
    deep = b"[" * 200_000 + b"]" * 200_000
    assert len(deep) < m.MAX_LINE
    r = _send(_server(), deep)
    assert r["error"]["code"] == m.PARSE_ERROR and r["id"] is None
    s = _server()
    _init(s)
    nested = b'{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"t","arguments":'
    out = s.handle_line(nested + b"[" * 200_000 + b"]" * 200_000 + b"}}")
    assert json.loads(out)["error"]["code"] == m.PARSE_ERROR
