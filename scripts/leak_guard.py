"""Leak guard for a public repository (spec §6). Not shipped in the package.

Three modes:
- `--diff FILE` scans only the lines a pull request ADDS;
- `--tree` scans every file git tracks, so content older than the pull request is seen too
  (#74; CI runs it on every pull request and every push to main);
- file mode scans the files named on the command line.

Private patterns come from the `LEAK_GUARD_PRIVATE_LIST` env var (CI secret) and from
`.leakguard-local.txt` (local, git-ignored), one regex per line. Hits from them are
reported by position only, never with the pattern or the text.

The local list itself must never be committed: a tracked `.leakguard-local.txt` (tree
mode) or one a diff adds or renames into place (diff mode) is a hit by its path alone,
with or without a private list in the context. A pattern's own source text copied into
any other file is a hit too, because a regex rarely matches its own source.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

LOCAL_DENY_LIST = ".leakguard-local.txt"
LOCAL_LIST_TRACKED = "local deny list is tracked; it must never be committed"

# RFC 2606 reserved names plus localhost; subdomains of these are allowed too
ALLOWED_EMAIL_DOMAINS = (
    "example.com",
    "example.org",
    "example.net",
    "example",
    "invalid",
    "localhost",
)
# Vendor SYSTEM mailboxes used as fixtures: exact local parts, never a whole domain, so a
# person at the same vendor still hits. Extending this list is a reviewed change.
SYSTEM_ADDRESS_RE = re.compile(
    r"^(?:"
    r"newsletter(?:-\d+)?@linkedin\.com"
    r"|(?:noreply|notifications|automated-\d+)@github\.com"
    r"|(?:mailer-daemon|bounce-\d+|noreply)@ionos\.de"
    r"|news@medium\.com"
    r")$",
    re.IGNORECASE,
)
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@((?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,}|localhost)\b")
IBAN_RE = re.compile(r"\b([A-Z]{2}\d{2}(?: ?[A-Z0-9]{4}){2,7}(?: ?[A-Z0-9]{1,3})?)\b")
PHONE_RE = re.compile(
    r"(?<![\w.])(?:\+|00)\d{1,3}"  # + or 00, country code
    r"[ /-]?\(?\d{2,5}\)?[ /-]?\d{3,}(?:[ /-]?\d+)*"  # area code, number
)
_HUNK_RE = re.compile(r"^@@ -\d+(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_DIFF_GIT_RE = re.compile(r"^diff --git (.+) (.+)$")
_BINARY_DIFFER_RE = re.compile(r"^Binary files (.+) and (.+) differ$")
# git's C-style path quoting (core.quotepath): \" \\ \a \b \f \n \r \t \v, else \ooo (octal byte)
_C_ESCAPES = {
    '"': 0x22,
    "\\": 0x5C,
    "a": 0x07,
    "b": 0x08,
    "f": 0x0C,
    "n": 0x0A,
    "r": 0x0D,
    "t": 0x09,
    "v": 0x0B,
}


class LeakGuardError(Exception):
    """A diff could not be parsed safely; fail closed rather than silently under-scan."""


@dataclass(frozen=True)
class Hit:
    path: str
    line: int
    kind: str  # "public" | "private"
    label: str  # "email" | "iban" | "phone" | "private-list hit"


@dataclass(frozen=True)
class AllowEntry:
    path: str
    line_re: re.Pattern


def parse_allow(text: str) -> list[AllowEntry]:
    """Lines: `<path>:<line regex> # <reason>`. A missing reason is an error."""
    entries = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        spec, sep, reason = line.partition(" # ")
        if not sep or not reason.strip():
            raise ValueError(f"allowlist entry needs ' # <reason>': {line!r}")
        path, _, rx = spec.partition(":")
        if not rx.strip():
            raise ValueError(f"allowlist entry needs a non-empty regex: {line!r}")
        entries.append(AllowEntry(path=path.strip(), line_re=re.compile(rx)))
    return entries


def _unquote_git_path(raw: str) -> str:
    """Undo git's C-style path quoting, e.g. `"b/t\\303\\244st.md"` -> `b/täst.md`.

    Octal escapes (`\\ooo`) are raw bytes, so the whole quoted body is rebuilt as
    bytes first and decoded as UTF-8 at the end (a multi-byte character is written
    as one octal escape per byte).
    """
    if len(raw) < 2 or raw[0] != '"' or raw[-1] != '"':
        return raw
    body = raw[1:-1]
    out = bytearray()
    i, n = 0, len(body)
    while i < n:
        c = body[i]
        if c == "\\":
            octal = body[i + 1 : i + 4]
            if len(octal) == 3 and all(d in "01234567" for d in octal):
                out.append(int(octal, 8) & 0xFF)
                i += 4
                continue
            nxt = body[i + 1] if i + 1 < n else ""
            if nxt in _C_ESCAPES:
                out.append(_C_ESCAPES[nxt])
                i += 2
                continue
        out.extend(c.encode("utf-8"))
        i += 1
    return out.decode("utf-8", errors="replace")


def _diff_lines(diff_text: str) -> list[str]:
    """Split `diff_text` on `"\\n"` only, and strip at most one trailing `"\\r"`
    per line.

    `str.splitlines()` also breaks on `\\x0b \\x0c \\x1c-\\x1e \\x85 \\u2028 \\u2029`
    and a lone `\\r` -- but git's diff *stream* format only ever ends a line with
    `"\\n"`. An added line whose own CONTENT contains one of those other break
    characters (e.g. an embedded `\\x0c` or a lone `\\r`) is git's ONE diff line;
    splitting on it too turns it into two fragments, desyncing every hunk's
    declared line count for the rest of that hunk (round-2 finding: the tail end
    of the hunk then reads as "outside a hunk" and is silently dropped).

    A trailing `"\\r"` *is* stripped: measured 2026-09-24, git reproduces a
    CRLF-terminated source file's own line ending faithfully in `+`/`-` content
    (`"+content\\r\\n"`), so a single trailing `\\r` is a real line-ending
    artifact, not scannable content, on every kind of line (control lines are
    LF-only per the same measurement, so stripping is a no-op there).
    """
    lines = diff_text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()  # the empty tail from a trailing "\n" -- not an extra line
    return [ln[:-1] if ln.endswith("\r") else ln for ln in lines]


def added_lines(diff_text: str) -> list[tuple[str, int, str]]:
    """(path, new line number, text) for every '+' line of a `git diff -U0`.

    A small state machine, not a per-line prefix check: `+++ `/`--- ` are read as
    file headers only between hunks (outside any hunk's declared line count) and
    `+++ ` only directly after a `--- ` line -- otherwise an added line whose own
    text happens to start with `++ ` (raw diff line `+++ ...`) would be misread as
    a new file header and silently end scanning for the rest of that file.

    Fails closed (`LeakGuardError`) on:
    - a `+++` target that unquotes to neither `b/...` nor `/dev/null`;
    - a line inside a hunk's declared count that starts with none of `+ - ' ' \\`;
    - a line outside a hunk that starts with `+` or `-` but is not a recognised
      `---`/`+++` header (e.g. a hunk header declaring 0+0 lines followed by a
      `+` line -- the `+` line would otherwise read as "outside a hunk" and be
      silently ignored);
    - a mismatch between the number of `+` lines seen inside hunks and the
      number of entries actually scanned (a sanity invariant covering any path
      this state machine did not anticipate, e.g. a `+` line under a `/dev/null`
      target).
    """
    out: list[tuple[str, int, str]] = []
    path: str | None = None
    n = 0
    remaining = 0  # lines left in the current hunk (old_count + new_count)
    after_minus = False
    plus_count = 0
    for line in _diff_lines(diff_text):
        if remaining > 0:
            if line.startswith("\\"):
                continue  # "\ No newline at end of file" -- not a counted hunk line
            if line.startswith("+"):
                plus_count += 1
                if path is not None:
                    out.append((path, n, line[1:]))
                n += 1
                remaining -= 1
            elif line.startswith("-"):
                remaining -= 1
            elif line.startswith(" "):
                n += 1
                remaining -= 1
            else:
                raise LeakGuardError(f"malformed hunk line near {path!r}:{n}")
            continue
        if line.startswith("diff --git "):
            path = None
            after_minus = False
            continue
        if line.startswith("--- "):
            after_minus = True
            continue
        if line.startswith("+++ ") and after_minus:
            after_minus = False
            target = line[4:]
            if target == "/dev/null":
                path = None
                continue
            unquoted = _unquote_git_path(target)
            if not unquoted.startswith("b/"):
                raise LeakGuardError(f"unrecognized diff target: {target!r}")
            path = unquoted[2:]
            continue
        after_minus = False
        m = _HUNK_RE.match(line)
        if m:
            old_count = int(m.group(1) or "1")
            n = int(m.group(2))
            new_count = int(m.group(3) or "1")
            remaining = old_count + new_count
            continue
        if line.startswith("+") or line.startswith("-"):
            raise LeakGuardError(f"unexpected diff line outside a hunk (near {path!r})")
    if plus_count != len(out):
        raise LeakGuardError(
            f"added-line count mismatch: {plus_count} '+' line(s) in hunks, {len(out)} scanned"
        )
    return out


def binary_hit_paths(diff_text: str) -> list[str]:
    """New-side paths of every undiffed binary entry (`Binary files ... differ` or
    `GIT binary patch`) in `diff_text`. A pure deletion (new side `/dev/null`) is
    excluded -- removing a binary file adds no content to scan.
    """
    paths: list[str] = []
    current: str | None = None
    for line in _diff_lines(diff_text):
        m = _DIFF_GIT_RE.match(line)
        if m:
            b = _unquote_git_path(m.group(2))
            current = b[2:] if b.startswith("b/") else None
            continue
        m = _BINARY_DIFFER_RE.match(line)
        if m:
            b_token = m.group(2)
            if b_token != "/dev/null":
                b = _unquote_git_path(b_token)
                paths.append(b[2:] if b.startswith("b/") else b)
            continue
        if line.startswith("GIT binary patch") and current is not None:
            paths.append(current)
    return paths


def diff_new_paths(diff_text: str) -> list[str]:
    """New-side path of every `diff --git` entry, including an empty new file, which
    has no hunk and no `+++` header. With unquoted paths that contain spaces the
    split is ambiguous; the basename after the last space is still exact."""
    paths: list[str] = []
    for line in _diff_lines(diff_text):
        m = _DIFF_GIT_RE.match(line)
        if m:
            b = _unquote_git_path(m.group(2))
            paths.append(b[2:] if b.startswith("b/") else b)
    return paths


def local_list_hits(paths) -> list[Hit]:
    """One hit per path that is the local deny list, by name, in any folder."""
    return [
        Hit(p, 0, "public", LOCAL_LIST_TRACKED) for p in paths if Path(p).name == LOCAL_DENY_LIST
    ]


def path_allowed(path: str, allow: list[AllowEntry]) -> bool:
    """True if `path` itself is covered by an allowlist entry, regardless of its
    regex (used for binary/undiffed entries, which have no line text to match).
    """
    name = Path(path).name
    return any(a.path in (path, name) for a in allow)


def _iban_valid(candidate: str) -> bool:
    s = candidate.replace(" ", "")
    if not 15 <= len(s) <= 34:
        return False
    digits = "".join(str(int(c, 36)) for c in s[4:] + s[:4])
    return int(digits) % 97 == 1


def _allowed(path: str, text: str, allow: list[AllowEntry]) -> bool:
    name = Path(path).name
    return any(a.path in (path, name) and a.line_re.search(text) for a in allow)


def _email_allowed(domain: str) -> bool:
    d = domain.lower()
    return any(d == a or d.endswith("." + a) for a in ALLOWED_EMAIL_DOMAINS)


def scan_lines(entries, private_patterns, allow) -> list[Hit]:
    sources = [p for p in private_patterns if p.strip()]
    private = [re.compile(p, re.IGNORECASE) for p in sources]
    hits: list[Hit] = []
    for path, n, line in entries:
        if _allowed(path, line, allow):
            continue
        if any(
            not _email_allowed(m.group(1)) and not SYSTEM_ADDRESS_RE.match(m.group(0))
            for m in EMAIL_RE.finditer(line)
        ):
            hits.append(Hit(path, n, "public", "email"))
        if any(_iban_valid(m.group(1)) for m in IBAN_RE.finditer(line)):
            hits.append(Hit(path, n, "public", "iban"))
        # digit groups inside an IBAN ("... 0044 0532 ...") must not read as a phone number
        if PHONE_RE.search(IBAN_RE.sub(" ", line)):
            hits.append(Hit(path, n, "public", "phone"))
        if any(rx.search(line) for rx in private):
            hits.append(Hit(path, n, "private", "private-list hit"))
        elif any(src in line for src in sources):
            hits.append(Hit(path, n, "private", "private-list source"))
    return hits


def scan_files(paths, private_patterns, allow) -> list[Hit]:
    entries = []
    for p in map(Path, paths):
        try:
            text = p.read_text(encoding="utf-8")
        except (UnicodeDecodeError, FileNotFoundError, IsADirectoryError):
            continue
        entries += [(str(p), n, line) for n, line in enumerate(text.splitlines(), start=1)]
    return scan_lines(entries, private_patterns, allow)


def report(hits: list[Hit]) -> None:
    for h in hits:
        if h.kind == "private":
            # a fixed label, never the pattern or the text
            print(f"{h.path}:{h.line}: {h.label}")
        else:
            print(f"{h.path}:{h.line}: {h.label}")


def _parse_private_list(raw: str) -> list[str]:
    """One regex per line; strip each (a pasted secret often carries a trailing
    space or CRLF artifact that would otherwise silently disable that entry) and
    drop empty lines.
    """
    return [ln.strip() for ln in raw.splitlines() if ln.strip()]


def _tracked_files() -> list[str]:
    """Every file git tracks in the current directory's repository. The local deny list
    is not skipped: if it is tracked, that is the finding (`local_list_hits`)."""
    out = subprocess.run(
        ["git", "ls-files", "-z"], check=True, capture_output=True, text=True
    ).stdout
    return [f for f in out.split("\0") if f]


def main(argv: list[str]) -> int:
    raw = os.environ.get("LEAK_GUARD_PRIVATE_LIST", "")
    local = Path(LOCAL_DENY_LIST)
    if local.is_file():
        raw += "\n" + "\n".join(
            ln
            for ln in local.read_text(encoding="utf-8").splitlines()
            if not ln.lstrip().startswith("#")
        )
    private = _parse_private_list(raw)
    if not private:
        print("leak-guard: private list not available in this context; public patterns only")
    allow_file = Path(".leak-guard-allow")
    allow = parse_allow(allow_file.read_text(encoding="utf-8")) if allow_file.exists() else []
    if argv[:1] == ["--diff"]:
        # Bytes, not read_text(): --text can force genuinely non-UTF-8 (binary)
        # content into the diff stream, and Path.read_text(encoding="utf-8") would
        # raise before the allowlist is even consulted. errors="replace" turns an
        # invalid byte into U+FFFD -- a garbled scanned line, never a crash.
        diff_text = Path(argv[1]).read_bytes().decode("utf-8", errors="replace")
        try:
            entries = added_lines(diff_text)
        except LeakGuardError as exc:
            print(f"leak-guard: {exc}")
            return 1
        blocked = [p for p in binary_hit_paths(diff_text) if not path_allowed(p, allow)]
        if blocked:
            for p in blocked:
                print(f"leak-guard: undiffed binary change blocked (not allowlisted): {p}")
            return 1
        hits = local_list_hits(diff_new_paths(diff_text)) + scan_lines(entries, private, allow)
        scope = f"{len(entries)} added line(s)"
    elif argv == ["--tree"]:
        files = _tracked_files()
        hits = local_list_hits(files) + scan_files(files, private, allow)
        scope = f"{len(files)} file(s)"
    elif any(a.startswith("--") for a in argv):
        print(f"leak-guard: unknown option {argv[0]!r}; use --diff FILE, --tree or file paths")
        return 2
    else:
        hits = scan_files(argv, private, allow)
        scope = f"{len(argv)} file(s)"
    report(hits)
    print(f"leak-guard: {scope} scanned, {len(hits)} hit(s)")
    return 1 if hits else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
