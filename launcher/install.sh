#!/bin/bash
# Provisioning of the bot on the target Mac (#76, L3, L4, L5, L6, L10, release condition 4).
# Runs only there, only as root, and only with the owner's OK. It installs and writes; it
# does NOT load the daemons and does NOT touch /etc/pf.conf. Those are separate steps
# with the owner, after James has read this.
#
#   sudo launcher/install.sh install <owner-user>
#   sudo launcher/install.sh status
#
# The tree this script sits in is what gets installed. It reaches the target Mac the way
# the keychain probe did: `git archive` of the head James read, sha256 compared before it
# is unpacked (run sheet, docs/2026-11-02-keychain-probe-runsheet.md). <owner-user>
# receives the run files (L5) and is given at the machine, so no account name is written
# into this public repository.
set -euo pipefail

HOST_WANTED="M1mini"
PREFIX="/usr/local/mailbox-autopilot"
BASE="/Library/Application Support/mailbox-autopilot"
BOT_USER="mailbox-autopilot-bot"
BOT_GROUP="mailbox-autopilot-bot"
DAEMONS="/Library/LaunchDaemons"
LABEL="ai.neckarshore.mailbox-autopilot"
PF_ANCHOR="/etc/pf.anchors/$LABEL"

# ---- pinned; tests/test_launcher_provision.py holds them against ci.yml, probe.sh, gate.py ----
UV_VERSION="0.13.5"
UV_SHA256="743f85035a1996ecb350659e01cd9faea9709829be09b1b4b03c73eaf3c9cd73"
UV_TARBALL="uv-aarch64-apple-darwin.tar.gz"
CLAUDE_VERSION="2.1.317"
# platforms.darwin-arm64.checksum of the release manifest, whose signature verified against
# the release key 31DDDE24DDFAB679F42D7BD2BAA929FF1A7ECACE on 2026-11-03
CLAUDE_SHA256="09f153b46b5df9e8b3454716d682a91901de020cbca630f61dec1a015b32b5e9"
CLAUDE_URL="https://downloads.claude.ai/claude-code-releases/$CLAUDE_VERSION/darwin-arm64/claude"
# ---- end pinned ----

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$SRC/.." && pwd)"
UV="$PREFIX/uv/uv"

say() { printf '%s\n' "install: $*"; }
die() {
  printf '%s\n' "install: $*" >&2
  exit 2
}

guard() {
  [ "$(uname -s)" = "Darwin" ] || die "this runs on the target Mac only"
  [ "$(hostname -s)" = "$HOST_WANTED" ] || die "this runs on $HOST_WANTED only, not $(hostname -s)"
  [ "$(id -u)" = "0" ] || die "run it with sudo"
}

fetch_checked() { # url sha256 out
  curl --fail --location --silent --show-error --output "$3" "$1"
  echo "$2  $3" | shasum -a 256 --check --quiet || die "checksum mismatch for $1"
}

install_uv() {
  say "uv $UV_VERSION, by checksum (L10)"
  local tmp
  tmp="$(mktemp -d)"
  fetch_checked "https://github.com/astral-sh/uv/releases/download/$UV_VERSION/$UV_TARBALL" \
    "$UV_SHA256" "$tmp/$UV_TARBALL"
  tar -xzf "$tmp/$UV_TARBALL" -C "$tmp"
  install -d -o root -g wheel -m 755 "$PREFIX/uv"
  install -o root -g wheel -m 755 "$tmp/uv-aarch64-apple-darwin/uv" "$UV"
  rm -rf "$tmp"
  UV_PYTHON_INSTALL_DIR="$PREFIX/uv/python" UV_CACHE_DIR="$PREFIX/uv/cache" \
    "$UV" --no-config python install --no-bin 3.11
  chmod -R a+rX "$PREFIX/uv/python"
}

install_claude() {
  say "Claude Code $CLAUDE_VERSION, by the signed manifest's checksum (L4)"
  local tmp
  tmp="$(mktemp -d)"
  fetch_checked "$CLAUDE_URL" "$CLAUDE_SHA256" "$tmp/claude"
  install -d -o root -g wheel -m 755 "$PREFIX/claude"
  install -o root -g wheel -m 755 "$tmp/claude" "$PREFIX/claude/claude"
  rm -rf "$tmp"
}

install_app() {
  say "the package from $REPO, frozen lock, no dev dependencies (release condition 3)"
  [ -f "$REPO/uv.lock" ] && [ -f "$REPO/pyproject.toml" ] || die "$REPO is not the package tree"
  [ ! -e "$REPO/.venv" ] || die "$REPO has a .venv; install from a clean git archive"
  rm -rf "$PREFIX/app"
  install -d -o root -g wheel -m 755 "$PREFIX/app"
  (cd "$REPO" && tar -cf - --exclude ./.git .) | tar -xf - -C "$PREFIX/app"
  env -i PATH=/usr/bin:/bin HOME=/var/root \
    UV_PYTHON_INSTALL_DIR="$PREFIX/uv/python" UV_CACHE_DIR="$PREFIX/uv/cache" \
    UV_PYTHON_DOWNLOADS=never UV_PROJECT_ENVIRONMENT="$PREFIX/app/.venv" \
    "$UV" --no-config sync --frozen --no-dev --python 3.11 --python-preference only-managed \
    --project "$PREFIX/app"
  chown -R root:wheel "$PREFIX/app"
  chmod -R a+rX,go-w "$PREFIX/app"
}

install_launcher() { # owner
  say "launcher files, root-owned (L3)"
  install -d -o root -g wheel -m 755 "$BASE" "$BASE/launcher" "$BASE/launcher/empty"
  install -o root -g wheel -m 755 "$SRC/launch.sh" "$BASE/launcher/launch.sh"
  install -o root -g wheel -m 644 "$SRC/gate.py" "$SRC/digest.py" "$SRC/retention.py" \
    "$SRC/prompt.txt" "$SRC/settings.json" "$SRC/host.json" "$BASE/launcher/"
  say "run files and digests go to $1 only (L5)"
  install -d -o "$1" -g staff -m 700 "$BASE/runs" "$BASE/digests"
  # C6: launchd creates a missing StandardErrorPath file 0644 whatever Umask says, and
  # appends to an existing one without changing its mode (measured 2026-11-25).
  say "stderr files root-only before launchd opens them (L13, L5)"
  for f in launcher.stderr retention.stderr; do
    [ ! -L "$BASE/$f" ] || die "$BASE/$f is a symlink"
    [ -e "$BASE/$f" ] || install -o root -g wheel -m 600 /dev/null "$BASE/$f"
    chown root:wheel "$BASE/$f"
    chmod 600 "$BASE/$f"
  done
}

install_user() {
  # Every step is checked on every run (CodeRabbit 4507077128): an install interrupted
  # behind sysadminctl left an account without group, membership or hidden flag.
  dseditgroup -o read "$BOT_GROUP" >/dev/null 2>&1 ||
    dseditgroup -o create -r "mailbox-autopilot bot" "$BOT_GROUP"
  if id "$BOT_USER" >/dev/null 2>&1; then
    say "user $BOT_USER exists; group, membership, hidden flag and home checked again"
  else
    say "the bot user: not an admin, never logged in"
    # The password handling follows L11, which the keychain probe decides. Until then
    # sysadminctl prompts for it ('-'), so it never stands in a process list: type a long
    # throwaway and keep it nowhere. No login shell, hidden from the login window.
    say "sysadminctl asks for the bot's password now: type a long throwaway, keep it nowhere"
    sysadminctl -addUser "$BOT_USER" -fullName "Mailbox Autopilot Bot" -shell /usr/bin/false \
      -password -
  fi
  dscl . -create "/Users/$BOT_USER" IsHidden 1
  dseditgroup -o checkmember -m "$BOT_USER" "$BOT_GROUP" >/dev/null 2>&1 ||
    dseditgroup -o edit -a "$BOT_USER" -t user "$BOT_GROUP"
  [ -d "/Users/$BOT_USER" ] || createhomedir -c -u "$BOT_USER" >/dev/null
}

install_audit_log() {
  # An existing log is checked, not trusted (CodeRabbit 4507077134): a chflags that failed
  # on an earlier run left it without sappend, and the host then refuses every session.
  local log="$BASE/host-audit.log"
  [ ! -L "$log" ] || die "$log is a symlink"
  if [ ! -e "$log" ]; then
    say "host-audit.log: owned by the bot, system append-only (section 5.5)"
    install -o "$BOT_USER" -g "$BOT_GROUP" -m 600 /dev/null "$log"
    chflags sappend "$log"
  fi
  [ -f "$log" ] || die "$log is not a regular file"
  [ "$(stat -f %Su "$log")" = "$BOT_USER" ] || die "$log is not owned by $BOT_USER"
  [ "$(stat -f %Lp "$log")" = "600" ] || die "$log is not mode 600"
  case ",$(stat -f %Sf "$log")," in
    *,sappnd,*) say "host-audit.log: owner, mode and sappend checked" ;;
    *)
      say "host-audit.log had no sappend; set again"
      chflags sappend "$log"
      ;;
  esac
}

install_backup_exclusion() {
  # James, #102 departure 3: retention cannot reach a backup, so a backup keeps run files,
  # digests and the audit log for as long as it lives. Excluding them is the owner's
  # decision. tmutil covers Time Machine only; -p excludes the path, so a file rotation
  # replaces (stderr) stays excluded.
  local answer p
  say "Exclude the bot's records from Time Machine backups (runs, digests, audit log, stderr)?"
  read -r -p "[y/N] " answer </dev/tty || answer=""
  case "$answer" in
    y | Y)
      # retention.py creates stderr/ on its first run; it exists from here on instead
      [ -e "$BASE/stderr" ] || install -d -o root -g wheel -m 700 "$BASE/stderr"
      for p in "$BASE/runs" "$BASE/digests" "$BASE/host-audit.log" "$BASE/stderr" \
        "$BASE/launcher.stderr" "$BASE/retention.stderr"; do
        [ -e "$p" ] || die "$p is missing; the steps before should have made it"
        tmutil addexclusion -p "$p"
      done
      say "excluded from Time Machine; other backup tools are not touched"
      ;;
    *) say "not excluded; the records stay in any backup as long as it lives" ;;
  esac
}

install_daemons() {
  say "the two LaunchDaemons, written but NOT loaded"
  local py
  py="$(UV_PYTHON_INSTALL_DIR="$PREFIX/uv/python" "$UV" --no-config python find 3.11)"
  "$py" "$SRC/plists.py" launcher >"$DAEMONS/$LABEL.plist"
  "$py" "$SRC/plists.py" retention >"$DAEMONS/$LABEL.retention.plist"
  chown root:wheel "$DAEMONS/$LABEL.plist" "$DAEMONS/$LABEL.retention.plist"
  chmod 644 "$DAEMONS/$LABEL.plist" "$DAEMONS/$LABEL.retention.plist"
  plutil -lint "$DAEMONS/$LABEL.plist" "$DAEMONS/$LABEL.retention.plist"
}

install_pf() {
  say "pf anchor copied to $PF_ANCHOR; /etc/pf.conf is NOT changed (L6)"
  install -o root -g wheel -m 644 "$SRC/pf.anchor" "$PF_ANCHOR"
}

cmd_install() { # owner
  [ $# -eq 1 ] || die "usage: install.sh install <owner-user>"
  id "$1" >/dev/null 2>&1 || die "no user $1"
  export HOME=/var/root # sudo keeps the caller's HOME, and uv writes there
  install_uv
  install_claude
  install_app
  install_user
  install_launcher "$1"
  install_audit_log
  install_backup_exclusion
  install_daemons
  install_pf
  say "done. Not loaded, not enabled. Check with: sudo $0 status"
}

cmd_status() { # read-only
  ls -ld "$PREFIX" "$PREFIX"/* "$BASE" "$BASE"/* "$BASE/launcher"/* 2>&1 || true
  ls -lO "$BASE/host-audit.log" 2>&1 || true
  ls -l "$DAEMONS/$LABEL.plist" "$DAEMONS/$LABEL.retention.plist" "$PF_ANCHOR" 2>&1 || true
  "$UV" --version 2>&1 || true
  "$PREFIX/claude/claude" --version 2>&1 || true
  id "$BOT_USER" 2>&1 || true
  if launchctl print "system/$LABEL" >/dev/null 2>&1; then
    say "$LABEL IS LOADED"
  else
    say "$LABEL not loaded"
  fi
}

guard
case "${1:-}" in
  install) shift && cmd_install "$@" ;;
  status) cmd_status ;;
  *) die "usage: install.sh install <owner-user> | status" ;;
esac
