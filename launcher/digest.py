"""The owner's daily digest (#76 section 3 (i), L5), counted by root from the run files.

The bot cannot write: `--tools ""` offers no file tool and its settings deny Write. So the
digest is not the bot's text. gate.py calls build() after each session, for the day before,
and this file counts the lean records that root already copied into the run files. Nothing
the model wrote is read, so a sender cannot steer a count by what its domain or its mail
says, and no number depends on how well the model added up.

Content, owner decision 2026-11-10 on #76: the number of new mails, the ten most frequent
sender domains, newsletter candidates (`has_list_unsubscribe`), and the mails whose DKIM,
SPF or DMARC verdict failed. Lean fields only; a value of the wrong shape is never echoed.

Which day a mail belongs to: the day of the first run that saw it, by the run file's name.
Never the record's `date`: lean.py fills it from the Date header first, which the sender
writes, so a mail dated 1999 would drop out of every digest. A mail that arrives after the
day's last run counts on the next day.

Which results count: only a `search` with the `since` the launcher wrote into the prompt
(the run file's day minus one) and no flag filter. The bot chooses its own arguments, and a
`read` of an old UID or a search from an earlier day would make old mail look new, so those
add no record and no coverage. A bot that strays makes the digest say "at least".

How complete a day is: `search` returns at most 100 records, newest UID first, and has no
offset. The digest says an exact number only if every run of the day that searched saw
everything since the run before it: the previous searching run was no older than
yesterday, and the launcher's search was untruncated, or covered all four size buckets
untruncated, or reached down to a UID the previous run had seen. Otherwise it says
"at least". A day with no searching run says "at least 0".

Standard library only, nothing imported from the package, like gate.py.
"""

from __future__ import annotations

import json
import os
import re
import stat
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

SERVER = "mailbox"
SEARCH = f"mcp__{SERVER}__search"
READ = f"mcp__{SERVER}__read"
BUCKETS = frozenset({"small", "medium", "large", "huge"})
VERDICTS = frozenset(
    {"fail", "neutral", "none", "pass", "permerror", "policy", "softfail", "temperror", "unknown"}
)
FAILED = frozenset({"fail", "permerror", "softfail"})
METHODS = ("dkim", "dmarc", "spf")
TOP = 10
NO_DOMAIN = "(no domain)"

_RUN_NAME = re.compile(r"run-([0-9]{4})([0-9]{2})([0-9]{2})T[0-9]{6}Z(?:-uv)?\.jsonl", re.ASCII)
_ISO_DAY = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", re.ASCII)
_UID = re.compile(r"[0-9]{1,10}", re.ASCII)
_LABEL = r"(?!-)[a-z0-9-]{1,63}(?<!-)"
_DOMAIN = re.compile(rf"{_LABEL}(?:\.{_LABEL})+", re.ASCII)


@dataclass
class Collected:
    records: dict[str, dict] = field(default_factory=dict)
    complete: bool = False


def _answer(content: object) -> dict | None:
    if isinstance(content, list):  # the host's shape: text blocks
        texts = [b.get("text") for b in content if isinstance(b, dict) and b.get("type") == "text"]
        content = texts[0] if len(texts) == 1 else None
    if not isinstance(content, str):  # the shape Claude Code 2.1.331 writes
        return None
    try:
        answer = json.loads(content)
    except ValueError:
        return None
    if not isinstance(answer, dict) or not isinstance(answer.get("messages"), list):
        return None
    return answer


def _blocks(event: dict) -> list:
    message = event.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    return [b for b in content if isinstance(b, dict)] if isinstance(content, list) else []


@dataclass
class _Run:
    day: str  # from the file name, UTC
    seen: list[tuple[str, dict]] = field(default_factory=list)  # from the launcher's search
    searched: bool = False  # any search, including one the bot strayed with
    whole: bool = False  # an untruncated search, or all four buckets untruncated
    low_truncated: int | None = None  # lowest UID of a truncated unbucketed search

    @property
    def high(self) -> int | None:
        return max((int(uid) for uid, _ in self.seen), default=None)


def _expected_since(run_day: str) -> str:
    """The `since` the launcher wrote into the prompt for a run on `run_day`."""
    return (date.fromisoformat(run_day) - timedelta(days=1)).isoformat()


def _launchers_search(inp: object, expected: str) -> bool:
    """True for a search with the launcher's `since` and no flag filter: one that sees
    every mail since yesterday. The bot picks its own arguments, so any other search, and
    every `read`, counts no record and no coverage: an earlier `since` or a read of an old
    UID would make old mail look new."""
    if not isinstance(inp, dict) or "unseen" in inp or "flagged" in inp:
        return False
    return inp.get("since") == expected and inp.get("size") in (None, *BUCKETS)


def _regular_bytes(path: Path) -> bytes | None:
    """C9 (James, #113): the content of `path` only if it is a regular file, judged on the
    open descriptor. O_NOFOLLOW refuses a link the owner swaps in after collect()'s lstat,
    O_NONBLOCK keeps a swapped-in FIFO from blocking the open."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None
    with os.fdopen(fd, "rb") as f:
        if not stat.S_ISREG(os.fstat(f.fileno()).st_mode):
            return None
        return f.read()


def _read_run(path: Path) -> _Run | None:
    data = _regular_bytes(path)
    if data is None:
        return None
    y, m, d = _RUN_NAME.fullmatch(path.name).groups()
    run = _Run(day=f"{y}-{m}-{d}")
    expected = _expected_since(run.day)
    uses: dict[str, object] = {}
    buckets: set[str] = set()
    for line in data.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        if event.get("type") == "assistant":
            for b in _blocks(event):
                if b.get("type") == "tool_use" and b.get("name") == SEARCH:
                    uses[str(b.get("id"))] = b.get("input")
            continue
        if event.get("type") != "user":
            continue
        for b in _blocks(event):
            if b.get("type") != "tool_result" or b.get("is_error"):
                continue
            key = str(b.get("tool_use_id"))
            answer = _answer(b.get("content")) if key in uses else None
            if answer is None:
                continue
            run.searched = True
            inp = uses[key]
            if not _launchers_search(inp, expected):
                continue
            uids = []
            for r in answer["messages"]:
                uid = r.get("uid") if isinstance(r, dict) else None
                if isinstance(uid, str) and _UID.fullmatch(uid):
                    run.seen.append((uid, r))
                    uids.append(int(uid))
            truncated = answer.get("truncated") is not False
            size = inp.get("size")
            if not truncated and size is None:
                run.whole = True
            elif not truncated:
                buckets.add(size)
            elif size is None and uids:
                low = min(uids)
                run.low_truncated = (
                    low if run.low_truncated is None else min(run.low_truncated, low)
                )
    if buckets == BUCKETS:
        run.whole = True
    return run


def collect(runs_dir: Path, day: str) -> Collected:
    """The lean records first seen on `day`, by UID, and whether that day was seen whole.
    Run files are read in name order, which is time order. Symlinks are not followed."""
    out = Collected()
    if not runs_dir.is_dir():
        return out
    names = sorted(
        e
        for e in runs_dir.iterdir()
        if _RUN_NAME.fullmatch(e.name) and stat.S_ISREG(e.lstat().st_mode)
    )
    first: dict[str, tuple[str, dict]] = {}
    previous: _Run | None = None
    complete: bool | None = None
    for path in names:
        run = _read_run(path)
        if run is None:
            continue
        for uid, r in run.seen:
            first.setdefault(uid, (run.day, r))
        if not run.searched:
            continue
        if run.day == day:
            no_gap = previous is None or _expected_since(run.day) <= previous.day
            # search answers newest UID first: a page reaching down to what the previous
            # run saw proves that nothing between the two runs was skipped
            overlaps = (
                run.low_truncated is not None
                and previous is not None
                and previous.high is not None
                and run.low_truncated <= previous.high
            )
            complete = (complete is not False) and no_gap and (run.whole or overlaps)
        previous = run
    out.records = {uid: r for uid, (seen_on, r) in first.items() if seen_on == day}
    out.complete = bool(complete)
    return out


def _domain(value: object) -> str:
    if isinstance(value, str) and len(value) <= 253 and _DOMAIN.fullmatch(value):
        return value
    return NO_DOMAIN


def _verdict(auth: object, method: str) -> str:
    value = auth.get(method) if isinstance(auth, dict) else None
    return value if isinstance(value, str) and value in VERDICTS else "?"


def _ranked(counter: Counter) -> list[tuple[str, int]]:
    return sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))


def render(day: str, got: Collected, built_at: str) -> str:
    records = list(got.records.values())
    n = len(records) if got.complete else f"at least {len(records)}"
    domains = Counter(_domain(r.get("sender_domain")) for r in records)
    news = Counter(
        _domain(r.get("sender_domain")) for r in records if r.get("has_list_unsubscribe") is True
    )
    failed = []
    for uid in sorted(got.records, key=int):
        r = got.records[uid]
        verdicts = [_verdict(r.get("auth"), m) for m in METHODS]
        if FAILED & set(verdicts):
            failed.append((uid, _domain(r.get("sender_domain")), *verdicts))

    lines = [
        f"# Mailbox digest, {day}",
        "",
        f"Built {built_at} by the launcher from the bot's search results, lean fields only.",
        "Every mail of that day was seen."
        if got.complete
        else "Not every mail of that day may have been seen: the counts are lower bounds.",
        "",
        f"New mails: {n}",
        "",
        "## Ten most frequent sender domains",
        "",
    ]
    if domains:
        lines += ["| # | Domain | Mails |", "|---|--------|-------|"]
        lines += [f"| {i} | {d} | {c} |" for i, (d, c) in enumerate(_ranked(domains)[:TOP], 1)]
    else:
        lines.append("None.")
    lines += ["", "## Newsletter candidates", ""]
    if news:
        lines.append(f"Mails with a List-Unsubscribe header: {sum(news.values())}, from:")
        lines.append(", ".join(f"{d} ({c})" for d, c in _ranked(news)))
    else:
        lines.append("None.")
    lines += [
        "",
        "## Failed authentication",
        "",
        "Failed means fail, softfail or permerror in DKIM, DMARC or SPF.",
        "",
    ]
    if failed:
        lines += ["| UID | Domain | DKIM | DMARC | SPF |", "|-----|--------|------|-------|-----|"]
        lines += ["| " + " | ".join(row) + " |" for row in failed]
    else:
        lines.append("None.")
    return "\n".join(lines) + "\n"


def write(digests_dir: Path, day: str, text: str) -> Path:
    """Replace the day's digest whole: a fresh 0600 file, given to whoever owns the
    directory (the owner, set by install.sh), then renamed over the old one. O_EXCL and
    O_NOFOLLOW refuse a planted name, and a rename replaces a symlink, never follows it."""
    final = digests_dir / f"digest-{day}.md"
    fresh = digests_dir / f".digest-{day}.md.tmp"
    if fresh.is_symlink() or fresh.exists():
        fresh.unlink()
    fd = os.open(fresh, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        os.write(fd, text.encode("utf-8"))
        owner = digests_dir.stat().st_uid
        if owner != os.geteuid():
            os.fchown(fd, owner, -1)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(fresh, final)
    return final


def build(day: str, runs_dir: Path, digests_dir: Path, built_at: str | None = None) -> dict:
    got = collect(runs_dir, day)
    built_at = built_at or datetime.now().astimezone().isoformat(timespec="seconds")
    write(digests_dir, day, render(day, got, built_at))
    return {"day": day, "mails": len(got.records), "complete": got.complete}
