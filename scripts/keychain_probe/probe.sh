#!/bin/bash
# The keychain probe for #76 L11. Run sheet: docs/2026-11-02-keychain-probe-runsheet.md
#
# Throwaway user, throwaway group, one LaunchDaemon at a time, a canary item, no real
# password. Everything lands under one directory and `cleanup` removes it. Arm 8 (a real
# Claude token) is not here: the owner released arms 1 to 7.
set -euo pipefail

HOST_WANTED="M1mini"
DIR="/Library/mailbox-autopilot-probe"
USER_NAME="mbxprobe"
GROUP_NAME="mbxprobe-bot"
LABEL="ai.neckarshore.keychain-probe"
PLIST="/Library/LaunchDaemons/$LABEL.plist"
UV_VERSION="0.13.5" # .github/workflows/ci.yml; Homebrew cannot supply it (#76 L10)
UV_SHA256="743f85035a1996ecb350659e01cd9faea9709829be09b1b4b03c73eaf3c9cd73"
UV_TARBALL="uv-aarch64-apple-darwin.tar.gz"
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SECRET="$DIR/unlock.secret"
LOGIN_SECRET="$DIR/login.secret"
REPORT="$DIR/report.jsonl"
PY="$DIR/venv311/bin/python"
PY2="$DIR/venv312/bin/python"

say() { printf '%s\n' "probe: $*"; }
die() {
  printf '%s\n' "probe: $*" >&2
  exit 2
}

guard() {
  [ "$(uname -s)" = "Darwin" ] || die "this runs on the target Mac only"
  [ "$(hostname -s)" = "$HOST_WANTED" ] || die "this runs on $HOST_WANTED only, not $(hostname -s)"
  [ "$(id -u)" = "0" ] || die "run it with sudo"
}

emit_plist() { # interpreter out err mode job-arguments...
  local python="$1" out="$2" err="$3" mode="$4"
  shift 4
  local user=null program
  if [ "$mode" = "username" ]; then user="\"$USER_NAME\""; fi
  program=$("$PY" -c 'import json,sys; print(json.dumps(sys.argv[1:]))' \
    "$python" "$DIR/job.py" "$@")
  "$PY" "$DIR/job.py" --emit-plist \
    "{\"label\":\"$LABEL\",\"program\":$program,\"out\":\"$out\",\"err\":\"$err\",\"user\":$user}" \
    >"$PLIST"
  chown root:wheel "$PLIST"
  chmod 644 "$PLIST"
}

record() { # arm mode out err
  if [ -s "$3" ]; then
    cat "$3" >>"$REPORT"
    "$PY" "$DIR/show.py" "$3"
  else
    say "$1/$2 wrote no line; its stderr follows"
    cat "$4"
    printf '{"arm":"%s","mode":"%s","failed":"no output"}\n' "$1" "$2" >>"$REPORT"
  fi
}

run_job_with() { # interpreter arm mode action [job arguments]
  local python="$1" arm="$2" mode="$3" action="$4"
  shift 4
  local out="$DIR/out/$arm-$mode.out" err="$DIR/out/$arm-$mode.err" i
  : >"$out"
  : >"$err"
  chown "$USER_NAME" "$out" "$err"
  emit_plist "$python" "$out" "$err" "$mode" --arm "$arm" --mode "$mode" --action "$action" \
    --user "$USER_NAME" --group "$GROUP_NAME" --secret-file "$SECRET" "$@"
  launchctl bootstrap system "$PLIST"
  for i in $(seq 1 60); do
    if [ -s "$out" ]; then break; fi
    sleep 1
  done
  launchctl bootout "system/$LABEL" 2>/dev/null || true
  rm -f "$PLIST"
  record "$arm" "$mode" "$out" "$err"
}

run_job() { # arm mode action [job arguments]
  run_job_with "$PY" "$@"
}

home_ready() {
  if [ ! -d "/Users/$USER_NAME" ]; then createhomedir -c -u "$USER_NAME" >/dev/null; fi
  [ -d "/Users/$USER_NAME" ] || die "no home for $USER_NAME"
}

cmd_setup() {
  if id "$USER_NAME" >/dev/null 2>&1; then die "user $USER_NAME exists; run cleanup first"; fi
  [ ! -e "$DIR" ] || die "$DIR exists; run cleanup first"
  mkdir -p "$DIR/bin" "$DIR/out" "$DIR/tmp"
  chown -R root:wheel "$DIR"
  chmod 755 "$DIR" "$DIR/bin"
  chmod 1777 "$DIR/out"
  install -o root -g wheel -m 644 "$SRC/job.py" "$SRC/show.py" "$SRC/requirements.txt" "$DIR/"

  say "uv $UV_VERSION, by checksum"
  curl --fail --location --silent --show-error --output "$DIR/tmp/$UV_TARBALL" \
    "https://github.com/astral-sh/uv/releases/download/$UV_VERSION/$UV_TARBALL"
  echo "$UV_SHA256  $DIR/tmp/$UV_TARBALL" | shasum -a 256 --check --quiet
  tar -xzf "$DIR/tmp/$UV_TARBALL" -C "$DIR/tmp"
  install -o root -g wheel -m 755 "$DIR/tmp/uv-aarch64-apple-darwin/uv" "$DIR/bin/uv"

  say "two interpreters, the locked keyring"
  export UV_PYTHON_INSTALL_DIR="$DIR/python" UV_CACHE_DIR="$DIR/tmp/cache"
  "$DIR/bin/uv" --no-config python install --no-bin 3.11 3.12
  local v
  for v in 311:3.11 312:3.12; do
    "$DIR/bin/uv" --no-config venv --quiet --python-preference only-managed \
      --python "${v#*:}" "$DIR/venv${v%%:*}"
    "$DIR/bin/uv" --no-config pip install --quiet --python "$DIR/venv${v%%:*}/bin/python" \
      --require-hashes -r "$DIR/requirements.txt"
  done
  rm -rf "${DIR:?}/tmp"
  chmod -R a+rX "$DIR/python" "$DIR/venv311" "$DIR/venv312"

  say "two throwaway secrets, root only"
  (umask 077 && openssl rand -hex 24 >"$SECRET" && openssl rand -hex 8 >"$LOGIN_SECRET")
  chmod 400 "$SECRET" "$LOGIN_SECRET"

  say "the throwaway group and user: never logged in, not an admin"
  dseditgroup -o create -r "keychain probe" "$GROUP_NAME"
  sysadminctl -addUser "$USER_NAME" -fullName "Keychain Probe" -password "$(cat "$LOGIN_SECRET")"
  dseditgroup -o edit -a "$USER_NAME" -t user "$GROUP_NAME"
  say "done. Next: arm1"
}

cmd_arm1() {
  run_job arm1 drop read
  run_job arm1 username read
}

cmd_arm2() {
  home_ready
  run_job arm2-prepare drop prepare
  run_job arm2 drop read
  run_job arm2 username read
}

cmd_arm3() {
  home_ready
  run_job arm3a-prepare drop prepare
  run_job arm3a drop unlock-read
  run_job arm3b-prepare drop prepare --partition
  run_job arm3b drop unlock-read
}

cmd_arm6() {
  home_ready
  run_job arm6a-prepare drop prepare --no-trust --partition
  run_job arm6a drop unlock-read
  run_job arm6b-prepare drop prepare --partition
  run_job_with "$PY2" arm6b drop unlock-read
}

cmd_arm7() {
  home_ready
  run_job arm7-prepare drop prepare --partition
  run_job arm7 drop unlock-read --service mailbox-cleanup-probe-wrong
}

cmd_arm4_install() { # the plist stays in place, so launchd starts the job at boot
  home_ready
  run_job arm4-prepare drop prepare "$@"
  local out="$DIR/out/arm4-boot.out" err="$DIR/out/arm4-boot.err"
  : >"$out"
  : >"$err"
  emit_plist "$PY" "$out" "$err" drop --arm arm4 --mode drop --action unlock-read \
    --user "$USER_NAME" --group "$GROUP_NAME" --secret-file "$SECRET"
  say "installed for the next boot. Reboot, then: arm4-read"
}

cmd_arm4_read() {
  launchctl bootout "system/$LABEL" 2>/dev/null || true
  rm -f "$PLIST"
  record arm4 drop "$DIR/out/arm4-boot.out" "$DIR/out/arm4-boot.err"
}

cmd_arm5_reset() {
  home_ready
  run_job arm5-reset drop reset
  say "the one GUI login: user Keychain Probe, password $(cat "$LOGIN_SECRET")"
  say "then this line in that user's Terminal, then log out:"
  printf 'security add-generic-password -s mailbox-cleanup-probe -a probe@example.invalid -w CANARY -T %s\n' \
    "$("$PY" -c 'import os,sys; print(os.path.realpath(sys.executable))')"
}

cmd_arm5_read() {
  run_job arm5 drop read
  run_job arm5 username read
}

cmd_report() {
  cat "$REPORT"
}

cmd_status() {
  local bad=0
  if id "$USER_NAME" >/dev/null 2>&1; then say "user $USER_NAME exists"; bad=1; fi
  if dscl . -read "/Groups/$GROUP_NAME" >/dev/null 2>&1; then say "group $GROUP_NAME exists"; bad=1; fi
  if [ -e "$DIR" ]; then say "$DIR exists"; bad=1; fi
  if [ -e "$PLIST" ]; then say "$PLIST exists"; bad=1; fi
  if [ -e "/Users/$USER_NAME" ]; then say "/Users/$USER_NAME exists"; bad=1; fi
  if [ "$bad" = 0 ]; then say "no user, group, directory or daemon of the probe is left"; fi
}

cmd_cleanup() {
  launchctl bootout "system/$LABEL" 2>/dev/null || true
  rm -f "$PLIST"
  if [ -f "$REPORT" ]; then
    install -m 644 "$REPORT" /Users/Shared/keychain-probe-report.jsonl
    say "report kept at /Users/Shared/keychain-probe-report.jsonl"
  fi
  if id "$USER_NAME" >/dev/null 2>&1; then sysadminctl -deleteUser "$USER_NAME"; fi
  dseditgroup -o delete "$GROUP_NAME" 2>/dev/null || true
  rm -rf "${DIR:?}"
  rm -rf "/Users/${USER_NAME:?}"
  cmd_status
}

guard
case "${1:-}" in
  setup) cmd_setup ;;
  arm1) cmd_arm1 ;;
  arm2) cmd_arm2 ;;
  arm3) cmd_arm3 ;;
  arm4-install)
    shift
    cmd_arm4_install "$@"
    ;;
  arm4-read) cmd_arm4_read ;;
  arm5-reset) cmd_arm5_reset ;;
  arm5-read) cmd_arm5_read ;;
  arm6) cmd_arm6 ;;
  arm7) cmd_arm7 ;;
  report) cmd_report ;;
  cleanup) cmd_cleanup ;;
  status) cmd_status ;;
  *) die "usage: sudo $0 setup|arm1|arm2|arm3|arm6|arm7|arm4-install [--partition]|arm4-read|arm5-reset|arm5-read|report|cleanup|status" ;;
esac
