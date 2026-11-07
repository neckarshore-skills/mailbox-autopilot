#!/bin/bash
# The bot's launcher, shell half (#76, launch specification L10, L1).
#
# launchd starts this as root (slice 2: a LaunchDaemon with no UserName key, so the
# credential step can run as root and the child drops to the bot user in gate.py).
# Before anything else it checks uv by absolute path: the second field of `uv --version`
# must equal the version CI pins. This cannot run on a Python that uv supplied.
# Then it starts gate.py from an empty environment, offline, with no uv config.
set -euo pipefail

# ---- pinned; tests/test_launcher.py holds UV_VERSION equal to .github/workflows/ci.yml ----
UV_VERSION="0.13.5"
UV="/usr/local/mailbox-autopilot/uv/uv"
UV_HOME="/usr/local/mailbox-autopilot/uv"
GATE="/Library/Application Support/mailbox-autopilot/launcher/gate.py"
RUNS_DIR="/Library/Application Support/mailbox-autopilot/runs"
# ---- end pinned ----

refuse() { # one line to the run file (L5) and to stderr (L13), then a non-zero exit
  local line file owner
  line="{\"launcher\": {\"refused\": \"uv\", \"reason\": \"$1\"}}"
  printf '%s\n' "$line" >&2
  if [ -d "$RUNS_DIR" ] && [ ! -L "$RUNS_DIR" ]; then
    # The runs directory belongs to the owner, so root never follows or overwrites a name
    # in it: noclobber opens with O_EXCL, which also refuses a planted symlink.
    file="$RUNS_DIR/run-$(date -u +%Y%m%dT%H%M%SZ)-uv.jsonl"
    (set -o noclobber && umask 077 && printf '%s\n' "$line" >"$file") || exit 20
    owner="$(stat -c %u "$RUNS_DIR" 2>/dev/null || stat -f %u "$RUNS_DIR")" # GNU, then BSD
    chown -h "$owner" "$file" 2>/dev/null || true
  fi
  exit 20
}

[ -x "$UV" ] || refuse "no uv at $UV, expected $UV_VERSION"
found="$("$UV" --version 2>/dev/null | awk '{print $2}')" || found=""
[ "$found" = "$UV_VERSION" ] || refuse "uv version: expected $UV_VERSION, found ${found:-nothing}"

exec /usr/bin/env -i \
  PATH=/usr/bin:/bin \
  HOME=/var/empty \
  UV_PYTHON_INSTALL_DIR="$UV_HOME/python" \
  UV_CACHE_DIR="$UV_HOME/cache" \
  UV_PYTHON_DOWNLOADS=never \
  "$UV" run --no-config --no-project --offline --python 3.11 --python-preference only-managed \
  "$GATE"
