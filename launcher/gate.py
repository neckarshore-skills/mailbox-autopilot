"""The bot's launcher, Python half (#76, launch specification L1, L5, L8, L11 seam, L12).

launch.sh runs first: it checks uv (L10) and starts this file from an empty environment.
This file runs as root. It builds the literal `claude -p` invocation, drops to the bot
user for the child only, and watches the child's first event: if the session is offered
anything but the pinned tool list, it is killed before any tool can run.

Standard library only, and nothing is imported from the mailbox_cleanup package. The
pinned list lives here on purpose (L8): widening the bot takes two keys, the package and
this root-owned file. tests/test_launcher.py holds the literals against the host.

Not done here, by design:
- The credential step (L11) is a seam that refuses, so this launcher cannot start a
  session until the keychain probe has an answer. See credential_step().
- Root ownership, the plist, managed settings, pf and retention are slice 2.
"""

from __future__ import annotations

import json
import os
import selectors
import signal
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

# ---- pinned (L8): change only together with the host, and James reads the change ----
SERVER = "mailbox"
PINNED_TOOLS = frozenset({f"mcp__{SERVER}__search", f"mcp__{SERVER}__read"})

# ---- proposed paths and bounds (the builder proposes, the PR names them) ----
BASE = Path("/Library/Application Support/mailbox-autopilot")
# L4: the version is a literal here, like the uv pin in launch.sh. install.sh downloads
# exactly this release and checks it against the signed manifest's SHA256.
CLAUDE_VERSION = "2.1.317"
CLAUDE = Path("/usr/local/mailbox-autopilot/claude/claude")
MCP_CONFIG = BASE / "launcher" / "host.json"
SETTINGS = BASE / "launcher" / "settings.json"  # the bot's own settings, not managed (PR)
PROMPT_FILE = BASE / "launcher" / "prompt.txt"
RUNS_DIR = BASE / "runs"  # L5: owner-only files, one per run
WORK_DIR = BASE / "launcher" / "empty"  # 5c: no CLAUDE.md, no .claude/
BOT_USER = "mailbox-autopilot-bot"
BOT_GROUP = "mailbox-autopilot-bot"
BOT_HOME = Path("/Users/mailbox-autopilot-bot")
OWNER_USER = None  # set at provisioning (slice 2); None leaves the run file root-only
MAX_TURNS = 12  # L12
WALL_CLOCK_S = 300  # L12: the whole session
INIT_TIMEOUT_S = 60  # the first event must be system/init

# exit codes of the launcher (the PR states them)
EXIT_OK = 0
EXIT_CREDENTIAL = 10  # L11 seam, or a credential that could not be read
EXIT_GATE = 11  # L8: the first event is not an init offering exactly the pinned list
EXIT_NO_INIT = 12  # no first event in time, or the stream ended before one
EXIT_WALL_CLOCK = 13  # L12
EXIT_CHILD = 14  # claude ended non-zero
EXIT_TERMINATED = 15  # SIGTERM, from launchd or root; the session was killed first
EXIT_VERSION = 16  # L4: the installed claude is not CLAUDE_VERSION


def build_env() -> dict[str, str]:
    """L1: the child's environment is built from nothing. No ANTHROPIC_*, no apiKeyHelper
    source, no ANTHROPIC_BASE_URL can come through, because nothing is copied.
    DISABLE_UPDATES blocks auto-updates, `claude update` and `claude install` (L4);
    DISABLE_AUTOUPDATER alone leaves the last two open (docs, setup)."""
    return {
        "PATH": "/usr/bin:/bin",
        "HOME": str(BOT_HOME),
        "LANG": "en_US.UTF-8",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
        "DISABLE_UPDATES": "1",
    }


def check_claude_version(claude: Path, env: dict[str, str], *, drop: bool = True) -> str | None:
    """L4: None when `claude --version` names CLAUDE_VERSION. Run as the bot user, with
    the same empty environment, before anything that could reach the network."""
    kwargs = {"user": BOT_USER, "group": BOT_GROUP, "extra_groups": [BOT_GROUP]} if drop else {}
    try:
        done = subprocess.run(
            [str(claude), "--version"],
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=30,
            **kwargs,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"claude --version failed: {type(exc).__name__}"
    found = done.stdout.decode("utf-8", "replace").split()[:1]
    if done.returncode != 0 or found != [CLAUDE_VERSION]:
        return f"claude version: expected {CLAUDE_VERSION}, found {' '.join(found) or 'nothing'}"
    return None


def kill_leftovers(user: str = BOT_USER) -> int:
    """A SIGKILL to the launcher skips every `finally`, and launchd does not reap a child in
    its own process group. So each run first kills whatever the bot user still runs: an
    orphan lives at most until the next run, and two sessions never overlap."""
    return subprocess.run(["/usr/bin/pkill", "-KILL", "-U", user], check=False).returncode


def build_argv(
    claude: Path = CLAUDE, mcp_config: Path = MCP_CONFIG, settings: Path = SETTINGS
) -> list[str]:
    """L1, L2, L8, L12: the literal invocation. The prompt goes in on stdin, so no
    variadic flag can swallow it. Never --bare, never a permission bypass."""
    return [
        str(claude),
        "-p",
        "--tools",
        "",
        "--allowedTools",
        *sorted(PINNED_TOOLS),
        "--strict-mcp-config",
        "--mcp-config",
        str(mcp_config),
        "--disable-slash-commands",
        "--setting-sources",
        "",
        "--settings",
        str(settings),
        "--no-session-persistence",
        "--output-format",
        "stream-json",
        "--verbose",
        "--max-turns",
        str(MAX_TURNS),
    ]


def check_init(event: object) -> str | None:
    """L8: None when the first event is system/init and offers exactly the pinned tools
    from exactly one server. Otherwise the reason, which goes to the run file."""
    if not isinstance(event, dict):
        return "first event is not an object"
    if event.get("type") != "system" or event.get("subtype") != "init":
        return f"first event is {event.get('type')}/{event.get('subtype')}, not system/init"
    tools = event.get("tools")
    if not isinstance(tools, list) or not all(isinstance(t, str) for t in tools):
        return "init carries no tool list"
    if len(tools) != len(set(tools)) or set(tools) != PINNED_TOOLS:
        extra = sorted(set(tools) - PINNED_TOOLS)
        missing = sorted(PINNED_TOOLS - set(tools))
        return f"tools differ from the pinned list: extra {extra}, missing {missing}"
    servers = event.get("mcp_servers")
    if not isinstance(servers, list):
        return "init carries no server list"
    names = [s.get("name") if isinstance(s, dict) else s for s in servers]
    if names != [SERVER]:
        return f"servers differ from the pinned list: {names}"
    return None


def credential_step(env: dict[str, str]) -> dict[str, str]:
    """L11 seam. The keychain probe decides what goes here: the Claude token from a
    root-only file, and the dedicated keychain put on the search list and unlocked in the
    same job. Until then the launcher refuses, so a draft cannot start a session."""
    raise CredentialUnavailable("L11 is unanswered: the credential step is not built")


class CredentialUnavailable(Exception):
    pass


def open_run_file(runs_dir: Path | None = None, owner: str | None = OWNER_USER):
    """L5: one file per run, created 0600 and handed to the owner. The bot never holds it:
    root reads the child's stdout and writes here. The base directory is provisioned."""
    runs_dir = RUNS_DIR if runs_dir is None else runs_dir
    runs_dir.mkdir(mode=0o700, exist_ok=True)
    name = datetime.now(UTC).strftime("run-%Y%m%dT%H%M%SZ.jsonl")
    fd = os.open(runs_dir / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    if owner is not None:
        import pwd

        os.fchown(fd, pwd.getpwnam(owner).pw_uid, -1)
    return os.fdopen(fd, "w", encoding="utf-8")


def note(out, **fields) -> None:
    out.write(json.dumps({"launcher": fields}, sort_keys=True) + "\n")
    out.flush()


def _kill(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass  # macOS answers EPERM, not ESRCH, for a group with no live member left
    if proc.poll() is None:
        proc.kill()
    proc.wait()


def supervise(
    argv: list[str],
    env: dict[str, str],
    prompt: str,
    out,
    *,
    drop: bool = True,
    cwd: Path = WORK_DIR,
    wall_clock_s: float = WALL_CLOCK_S,
    init_timeout_s: float = INIT_TIMEOUT_S,
) -> int:
    """Start the child, gate on its first event, copy every line to the run file, and
    kill it at the wall clock. `drop=False` only in tests, which do not run as root.

    Fail closed: the child runs in its own process group, so whatever ends this function
    (a return, an exception, SIGTERM from launchd) kills that whole group on the way out,
    the MCP host included."""
    kwargs = {}
    if drop:
        # extra_groups is required: without it the child keeps root's supplementary groups.
        kwargs = {"user": BOT_USER, "group": BOT_GROUP, "extra_groups": [BOT_GROUP]}
    previous = signal.signal(signal.SIGTERM, _terminated)
    try:
        proc = subprocess.Popen(
            argv,
            env=env,
            cwd=cwd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            process_group=0,
            close_fds=True,
            **kwargs,
        )
        try:
            return _watch(proc, prompt, out, wall_clock_s, init_timeout_s)
        finally:
            _kill(proc)
    finally:
        signal.signal(signal.SIGTERM, previous)


class Terminated(Exception):
    pass


def _terminated(signum, frame):
    raise Terminated()


def _watch(proc, prompt: str, out, wall_clock_s: float, init_timeout_s: float) -> int:
    start = time.monotonic()
    try:
        proc.stdin.write(prompt.encode())
        proc.stdin.close()
    except BrokenPipeError:
        pass  # the child is gone; the stream below says what it left
    sel = selectors.DefaultSelector()
    sel.register(proc.stdout, selectors.EVENT_READ)
    buf = b""
    gated = False

    def deadline() -> float:
        return start + (wall_clock_s if gated else min(init_timeout_s, wall_clock_s))

    def expired() -> int:
        if gated:
            note(out, refused="wall_clock", seconds=wall_clock_s)
            return EXIT_WALL_CLOCK
        note(out, refused="no_init", reason=f"no event within {init_timeout_s}s")
        return EXIT_NO_INIT

    def take(line: bytes) -> str | None:
        nonlocal gated
        if not gated:
            try:
                reason = check_init(json.loads(line))
            except ValueError:
                reason = "first line is not JSON"
            if reason is not None:
                return reason
            gated = True
        out.write(line.decode("utf-8", "replace") + "\n")
        return None

    while True:
        now = time.monotonic()
        if now >= deadline():
            return expired()
        if not sel.select(timeout=deadline() - now):
            continue
        chunk = os.read(proc.stdout.fileno(), 65536)
        if not chunk:
            break
        buf += chunk
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            if (reason := take(line)) is not None:
                note(out, refused="gate", reason=reason)
                return EXIT_GATE
        out.flush()
    if buf and (reason := take(buf)) is not None:
        note(out, refused="gate", reason=reason)
        return EXIT_GATE
    if not gated:
        note(out, refused="no_init", reason="stream ended before an init event")
        return EXIT_NO_INIT
    try:
        rc = proc.wait(timeout=max(0.0, deadline() - time.monotonic()))
    except subprocess.TimeoutExpired:
        return expired()
    if rc != 0:
        note(out, child_exit=rc)
        return EXIT_CHILD
    return EXIT_OK


def main() -> int:
    with open_run_file() as out:
        note(out, leftovers_killed=kill_leftovers() == 0)
        if (reason := check_claude_version(CLAUDE, build_env())) is not None:
            note(out, refused="version", reason=reason)
            return EXIT_VERSION
        try:
            env = credential_step(build_env())
        except CredentialUnavailable as exc:
            note(out, refused="credential", reason=str(exc))
            return EXIT_CREDENTIAL
        prompt = PROMPT_FILE.read_text(encoding="utf-8")
        try:
            return supervise(build_argv(), env, prompt, out)
        except Terminated:
            note(out, refused="terminated")
            return EXIT_TERMINATED


if __name__ == "__main__":
    sys.exit(main())
