"""The bot profile's anchor and the bot's identity (#76, slice 3a).

An unattended bot runs as its own macOS user. Two root-controlled facts decide what it may
do, and neither comes from env, HOME or a file the bot can write:

- **Identity: membership in the group `BOT_GROUP`**, created by root at install. A member
  is the bot. The CLI refuses every member, whatever state the profile is in: the bot's
  only interface is the MCP host. A broken profile therefore does not lock the owner out.
- **The profile: a root-owned file at `ANCHOR_DIR/PROFILE_NAME`.** The anchor's parent,
  the anchor directory and the file must belong to `ANCHOR_OWNER_UID` (root) and be
  writable by nobody else; the file is no symlink. The host serves only a member whose
  name the valid profile carries, and refuses in every other state (`host_profile`).

An anchor directory without the group is an incomplete install: everyone is refused.

`ANCHOR_DIR`, `ANCHOR_OWNER_UID`, `BOT_GROUP` and `_home_of` are module attributes so the
tests can point them at a temporary tree. Nothing reads them from the environment.
"""

from __future__ import annotations

import grp
import hashlib
import json
import os
import pwd
import stat
from dataclasses import dataclass
from pathlib import Path

ANCHOR_DIR = Path("/Library/Application Support/mailbox-autopilot")
PROFILE_NAME = "profile.json"
ANCHOR_OWNER_UID = 0
BOT_GROUP = "mailbox-autopilot-bot"
MAX_PROFILE_BYTES = 64 * 1024
SCHEMA_VERSION = 1
READ_LEVELS = frozenset({"lean"})  # D7: the first rollout reads headers only
REFUSED_EXIT = 6  # exit codes 0-5 are taken (README)

_KEYS = frozenset({"schema_version", "bot_user", "account", "read_level"})


class ProfileRefused(Exception):
    """The caller may not proceed. `rule` is a stable name for the audit log and the JSON
    error; `sha256` is the profile's hash when a valid profile was read."""

    def __init__(self, rule: str, message: str, sha256: str | None = None):
        super().__init__(message)
        self.rule = rule
        self.sha256 = sha256


@dataclass(frozen=True)
class Profile:
    bot_user: str
    bot_uid: int
    account: str
    read_level: str
    sha256: str


# --- system lookups (replaced in tests) -------------------------------------------


def _bot_group() -> tuple[int, frozenset[str]] | None:
    try:
        g = grp.getgrnam(BOT_GROUP)
    except KeyError:
        return None
    return g.gr_gid, frozenset(g.gr_mem)


def _caller_gids() -> set[int]:
    """The process's groups. On macOS `os.getgroups()` is the user's directory-service
    group list, which the user cannot change."""
    return set(os.getgroups()) | {os.getgid(), os.getegid()}


def _user_name(uid: int) -> str:
    return pwd.getpwuid(uid).pw_name


def _home_of(uid: int) -> Path:
    """The caller's home from the user database, never from HOME."""
    return Path(pwd.getpwuid(uid).pw_dir)


# --- the anchor ---------------------------------------------------------------------


def _secure_dir(path: Path) -> bool:
    st = os.lstat(path)
    return (
        stat.S_ISDIR(st.st_mode)
        and st.st_uid == ANCHOR_OWNER_UID
        and not st.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
    )


def _invalid(message: str) -> ProfileRefused:
    return ProfileRefused("profile_invalid", f"bot profile invalid: {message}")


def _parse(data: bytes, sha256: str) -> Profile:
    try:
        doc = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise _invalid("not JSON") from None
    if not isinstance(doc, dict) or set(doc) != _KEYS:
        raise _invalid(f"must be an object with exactly the keys {sorted(_KEYS)}")
    version = doc["schema_version"]
    if type(version) is not int or version != SCHEMA_VERSION:
        raise _invalid(f"schema_version must be {SCHEMA_VERSION}")
    user, account, level = doc["bot_user"], doc["account"], doc["read_level"]
    if not isinstance(user, str) or not user:
        raise _invalid("bot_user must be a user name")
    try:
        uid = pwd.getpwnam(user).pw_uid
    except KeyError:
        raise _invalid("bot_user does not exist") from None
    if uid == 0:
        raise _invalid("bot_user may not be root")
    if not isinstance(account, str) or not account or not account.isprintable():
        raise _invalid("account must be a printable alias")
    if not isinstance(level, str) or level not in READ_LEVELS:
        raise _invalid(f"read_level must be one of {sorted(READ_LEVELS)}")
    return Profile(bot_user=user, bot_uid=uid, account=account, read_level=level, sha256=sha256)


def _root_only_file(st: os.stat_result) -> bool:
    return (
        stat.S_ISREG(st.st_mode)
        and st.st_uid == ANCHOR_OWNER_UID
        and not st.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
    )


def load_profile() -> Profile:
    """Read and validate the anchored profile, or raise ProfileRefused with the reason."""
    try:
        if not _secure_dir(ANCHOR_DIR.parent):
            raise ProfileRefused("anchor_insecure", "the anchor's parent is not root-only")
        if not _secure_dir(ANCHOR_DIR):
            raise ProfileRefused("anchor_insecure", "the anchor directory is not root-only")
    except FileNotFoundError:
        raise ProfileRefused("profile_not_installed", "no bot profile is installed") from None
    path = ANCHOR_DIR / PROFILE_NAME
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        raise ProfileRefused("profile_missing", "the anchor holds no profile") from None
    if not _root_only_file(st):
        raise ProfileRefused("profile_insecure", "the profile is not a root-only regular file")
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except PermissionError:
        raise ProfileRefused("profile_unreadable", "the profile cannot be read") from None
    try:
        # the same check on the opened descriptor: what is hashed and parsed is the file
        # that was checked, with no window between lstat and open (James, #89)
        if not _root_only_file(os.fstat(fd)):
            raise ProfileRefused("profile_insecure", "the profile changed while it was opened")
        data = os.read(fd, MAX_PROFILE_BYTES + 1)
    finally:
        os.close(fd)
    if len(data) > MAX_PROFILE_BYTES:
        raise _invalid(f"larger than {MAX_PROFILE_BYTES} bytes")
    return _parse(data, hashlib.sha256(data).hexdigest())


def _is_member(uid: int, group: tuple[int, frozenset[str]]) -> bool:
    gid, members = group
    if _user_name(uid) in members:
        return True
    return uid == os.getuid() and gid in _caller_gids()


def _sha_or_none() -> str | None:
    try:
        return load_profile().sha256
    except ProfileRefused:
        return None


# --- the two gates ------------------------------------------------------------------


def cli_gate(uid: int) -> None:
    """Raise ProfileRefused if the CLI must refuse this caller. Runs before click parses."""
    group = _bot_group()
    if group is not None and _is_member(uid, group):
        raise ProfileRefused(
            "cli_closed_to_bot",
            "this user is the mail bot; its interface is the MCP host, not the CLI",
            _sha_or_none(),
        )
    try:
        os.lstat(ANCHOR_DIR)
    except FileNotFoundError:
        return  # nothing installed: the CLI is unchanged
    if group is None:
        raise ProfileRefused(
            "bot_group_missing",
            f"a bot profile anchor exists but the group {BOT_GROUP} does not; "
            "the install is incomplete",
        )
    try:
        p = load_profile()
    except ProfileRefused:
        return  # the group, not the file, says who the bot is
    if p.bot_uid == uid:
        raise ProfileRefused(
            "bot_not_in_group",
            f"the profile names this user but it is not in {BOT_GROUP}",
            p.sha256,
        )


def host_profile(uid: int) -> Profile:
    """The MCP host's entry: the profile for the bot, or ProfileRefused. No unrestricted mode."""
    group = _bot_group()
    if group is None:
        raise ProfileRefused("bot_group_missing", f"the group {BOT_GROUP} does not exist")
    if not _is_member(uid, group):
        raise ProfileRefused("not_bot_user", f"the host runs only for members of {BOT_GROUP}")
    p = load_profile()
    if p.bot_uid != uid:
        raise ProfileRefused(
            "profile_names_other_user", "the profile names a different bot user", p.sha256
        )
    return p
