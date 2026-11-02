"""One arm of the keychain probe (#76, L11). Runs inside a LaunchDaemon on the target Mac.

It answers one question the launcher cannot start without: can a launchd job of a user
who never logs in read that user's keychain? `sudo -u`, `launchctl asuser` and SSH do not
count, because they run in another security session and can pass where the job fails.

Two modes, as James's read of 2026-11-02 asks (section 6):

- **drop** is the production shape: the daemon starts as root, reads the unlock secret,
  and drops to the user inside the same launchd job.
- **username** is the comparison: launchd starts the job as the user.

It prints one JSON line. The value it reads is compared with the canary and never
printed, and nothing here touches a real credential.
"""

from __future__ import annotations

import argparse
import datetime
import grp
import json
import os
import plistlib
import pwd
import subprocess
import sys
from pathlib import Path

SERVICE = "mailbox-cleanup-probe"
ACCOUNT = "probe@example.invalid"
CANARY = "CANARY"
SECURITY = "/usr/bin/security"
# What an interpreter outside Apple's own tools may need to read an item without a prompt:
# `add-generic-password` writes only Apple's partitions. Arm 3 has three halves: none (3a),
# `unsigned:` (3b) and the interpreter's own `cdhash:` (3c), because uv's Python is ad-hoc
# signed. Later arms use the one that read: a list that was not proven proves nothing.
BASE_PARTITIONS = "apple-tool:,apple:"


def _run(*argv: str) -> tuple[int, str]:
    r = subprocess.run(argv, capture_output=True, text=True, timeout=30)
    return r.returncode, r.stdout.strip()


def _keychains(text: str) -> list[str]:
    return [line.strip().strip('"') for line in text.splitlines() if line.strip()]


def _python() -> str:
    return os.path.realpath(sys.executable)


def facts(group: str) -> dict:
    """Who and where this job is. Every field is a measurement that L11 or the Identity
    line needs, and none of them is a secret."""
    home = os.environ.get("HOME", "")
    _, search = _run(SECURITY, "list-keychains")
    _, default = _run(SECURITY, "default-keychain")
    _, manager = _run("/bin/launchctl", "managername")
    _, console = _run("/usr/bin/stat", "-f", "%Su", "/dev/console")
    _, boot = _run("/usr/sbin/sysctl", "-n", "kern.boottime")
    me = pwd.getpwuid(os.getuid()).pw_name
    try:
        g = grp.getgrnam(group)
        gr_mem, in_groups = me in g.gr_mem, g.gr_gid in os.getgroups()
    except KeyError:
        gr_mem, in_groups = None, None
    return {
        "uid": os.getuid(),
        "euid": os.geteuid(),
        "user": me,
        "groups": sorted(os.getgroups()),
        "group_in_gr_mem": gr_mem,
        "group_in_getgroups": in_groups,
        "home_env": home,
        "home_exists": bool(home) and Path(home).is_dir(),
        "search_list": _keychains(search),
        "default_keychain": _keychains(default),
        "manager": manager,
        "console_user": console,
        "boottime": boot,
        "python": sys.executable,
        "python_real": _python(),
        "python_version": sys.version.split()[0],
        "codesign": [
            line
            for line in _codesign().splitlines()
            if line.startswith(("Identifier", "TeamId", "Sig", "CDHash"))
        ],
    }


def _codesign() -> str:
    return subprocess.run(
        ["/usr/bin/codesign", "-dvvv", _python()], capture_output=True, text=True, timeout=30
    ).stderr


def _partitions(kind: str) -> str:
    if kind == "unsigned":
        return f"{BASE_PARTITIONS},unsigned:"
    found = [x for x in _codesign().splitlines() if x.startswith("CDHash=")]
    return f"{BASE_PARTITIONS},cdhash:{found[0].split('=', 1)[1]}"


def _keychain_path() -> str:
    return os.path.join(os.environ["HOME"], "Library/Keychains/probe.keychain-db")


def security_script(lines: list[str], secret: str = "") -> dict:
    """Run `security` commands from stdin, so a secret never sits in argv. One failed
    line makes the return code 1 and prints `<command>: returned <n>`."""
    r = subprocess.run(
        [SECURITY, "-i"], input="\n".join(lines) + "\n", capture_output=True, text=True, timeout=60
    )
    errors = [
        line.replace(secret, "***") if secret else line
        for line in (r.stdout + r.stderr).splitlines()
        if "returned" in line or "error" in line.lower()
    ]
    return {"rc": r.returncode, "errors": errors[:6]}


def read_item(service: str) -> dict:
    """The host's own read path, `macOS.Keyring().get_password`. What comes back is
    compared and dropped."""
    from keyring import errors
    from keyring.backends.macOS import Keyring

    try:
        value = Keyring().get_password(service, ACCOUNT)
    except Exception as e:
        status = e.__cause__.args[0] if e.__cause__ is not None and e.__cause__.args else None
        kind = "other"
        if isinstance(e, errors.KeyringLocked):
            kind = "KeyringLocked"
        elif isinstance(e, errors.KeyringError):
            kind = "KeyringError"
        return {"outcome": kind, "osstatus": status, "value_equals_canary": False}
    if value is None:
        return {"outcome": "none", "osstatus": None, "value_equals_canary": False}
    return {"outcome": "value", "osstatus": None, "value_equals_canary": value == CANARY}


def _add_item(path: str, *, trust: bool) -> str:
    trusted = f" -T {_python()}" if trust else ""
    return f"add-generic-password -s {SERVICE} -a {ACCOUNT} -w {CANARY}{trusted} {path}"


def prepare(secret: str, *, trust: bool, partition: str) -> dict:
    """A fresh dedicated keychain with the canary in it, left locked. Done from inside the
    job, because the search list belongs to the session that sets it."""
    path = _keychain_path()
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    if os.path.exists(path):
        security_script([f"delete-keychain {path}"])
    lines = [
        f"create-keychain -p {secret} {path}",
        f"list-keychains -d user -s {path}",
        f"default-keychain -d user -s {path}",
        f"unlock-keychain -p {secret} {path}",
        _add_item(path, trust=trust),
    ]
    if partition != "none":
        lines.append(
            f"set-generic-password-partition-list -S {_partitions(partition)} -s {SERVICE} "
            f"-a {ACCOUNT} -k {secret} {path}"
        )
    lines.append(f"lock-keychain {path}")
    return security_script(lines, secret)


def unlock_and_read(secret: str, service: str) -> dict:
    path = _keychain_path()
    unlocked = security_script([f"unlock-keychain -p {secret} {path}"], secret)
    read = read_item(service)
    locked = security_script([f"lock-keychain {path}"])
    return {"unlock": unlocked, "read": read, "lock_again": locked}


def reset() -> dict:
    """Back to a user with no keychain of ours, before the one GUI login of arm 5."""
    path = _keychain_path()
    result = security_script([f"delete-keychain {path}"]) if os.path.exists(path) else {}
    prefs = Path(os.environ["HOME"]) / "Library/Preferences/com.apple.security.plist"
    prefs.unlink(missing_ok=True)
    return {"delete": result}


def drop_to(user: str) -> None:
    """Inside this job, so the process stays in launchd's system session."""
    p = pwd.getpwnam(user)
    os.initgroups(user, p.pw_gid)
    os.setgid(p.pw_gid)
    os.setuid(p.pw_uid)
    os.environ.clear()
    os.environ.update(HOME=p.pw_dir, USER=user, LOGNAME=user, PATH="/usr/bin:/bin:/usr/sbin:/sbin")
    if os.path.isdir(p.pw_dir):
        os.chdir(p.pw_dir)


def plist(*, label: str, program: list[str], out: str, err: str, user: str | None) -> bytes:
    """A LaunchDaemon shaped like the production one (#76 L3, L13)."""
    doc = {
        "Label": label,
        "ProgramArguments": program,
        "RunAtLoad": True,
        "StandardOutPath": out,
        "StandardErrorPath": err,
        "WorkingDirectory": "/",
    }
    if user:
        doc.update(UserName=user, InitGroups=True)
    return plistlib.dumps(doc)


def run_arm(args) -> dict:
    secret = ""
    if args.secret_file and os.geteuid() == 0:
        secret = Path(args.secret_file).read_text(encoding="utf-8").strip()
    if args.mode == "drop":
        drop_to(args.user)
    record: dict = {"arm": args.arm, "mode": args.mode, "action": args.action}
    record["at"] = datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds")
    record["facts"] = facts(args.group)
    if args.action == "prepare":
        record["prepare"] = prepare(secret, trust=not args.no_trust, partition=args.partition)
    elif args.action == "read":
        record["read"] = read_item(args.service)
    elif args.action == "unlock-read":
        record.update(unlock_and_read(secret, args.service))
    elif args.action == "reset":
        record["reset"] = reset()
    return record


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--arm", required=True)
    ap.add_argument("--mode", choices=("drop", "username"), required=True)
    ap.add_argument("--action", choices=("prepare", "read", "reset", "unlock-read"), required=True)
    ap.add_argument("--user", required=True)
    ap.add_argument("--group", required=True)
    ap.add_argument("--secret-file")
    ap.add_argument("--service", default=SERVICE)
    ap.add_argument("--no-trust", action="store_true")
    ap.add_argument("--partition", choices=("none", "unsigned", "cdhash"), default="none")
    args = ap.parse_args(argv)
    try:
        record = run_arm(args)
    except Exception as e:  # a failed arm is a measurement: it still prints its line
        record = {"arm": args.arm, "mode": args.mode, "failed": type(e).__name__}
    print(json.dumps(record, sort_keys=True), flush=True)
    return 0


if __name__ == "__main__":
    if sys.argv[1:2] == ["--emit-plist"]:
        sys.stdout.buffer.write(plist(**json.loads(sys.argv[2])))
        sys.exit(0)
    sys.exit(main())
