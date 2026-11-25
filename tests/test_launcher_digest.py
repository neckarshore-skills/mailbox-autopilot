"""#76 section 3 (i), L5: the daily digest, built by root from the run files.

The bot has no tool that writes (settings.json denies Write, and `--tools ""` offers
none), so the digest cannot be the bot's text. It is counted by launcher/digest.py from
the tool results root already copied into the run files. A sender domain is chosen by the
sender, so nothing the model wrote reaches the digest, and no count depends on it.

Content: owner decision 2026-11-10 on #76 (new-mail count, ten most frequent sender
domains, newsletter candidates, failed DKIM/SPF/DMARC verdicts; lean fields only)."""

import importlib.util
import json
import os
import re
import stat
import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
LAUNCHER = ROOT / "launcher"


def _load(name):
    spec = importlib.util.spec_from_file_location(f"launcher_{name}", LAUNCHER / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # a dataclass looks its module up there
    spec.loader.exec_module(module)
    return module


digest = _load("digest")

DAY = "2026-11-23"
SINCE = "2026-11-22"  # the launcher's "yesterday" for a run on DAY
D_RUN = "run-20261123T060012Z.jsonl"  # the first run of DAY
D_RUN2 = "run-20261123T150003Z.jsonl"
PREV = "run-20261122T190000Z.jsonl"  # the last run of the day before
NEXT = "run-20261124T060012Z.jsonl"  # the first run of the next day


def rec(uid, domain="example.org", date=DAY, unsub=False, auth=None):
    return {
        "auth": auth or {"dkim": "pass", "dmarc": "pass", "spf": "pass"},
        "auto_submitted": None,
        "categories": [],
        "date": date,
        "has_attachment": False,
        "has_list_id": unsub,
        "has_list_unsubscribe": unsub,
        "keep_hit": False,
        "precedence": None,
        "sender_domain": domain,
        "size": "small",
        "uid": uid,
    }


def tool_use(tid, name="mcp__mailbox__search", **inp):
    return {
        "type": "assistant",
        "message": {"content": [{"type": "tool_use", "id": tid, "name": name, "input": inp}]},
    }


def tool_result(tid, records, truncated=False, as_list=False, is_error=False):
    text = json.dumps(
        {"count": len(records), "messages": records, "truncated": truncated},
        separators=(",", ":"),
    )
    content = [{"type": "text", "text": text}] if as_list else text
    block = {"tool_use_id": tid, "type": "tool_result", "content": content}
    if is_error:
        block["is_error"] = True
    return {"type": "user", "message": {"role": "user", "content": [block]}}


def write_run(runs, name, events):
    runs.mkdir(exist_ok=True)
    path = runs / name
    path.write_text("".join((e if isinstance(e, str) else json.dumps(e)) + "\n" for e in events))
    return path


def search_run(runs, name, records, *, since=SINCE, truncated=False, **inp):
    return write_run(
        runs,
        name,
        [
            {"launcher": {"leftovers_killed": True}},
            tool_use("t1", since=since, limit=100, **inp),
            tool_result("t1", records, truncated),
            {"type": "result", "subtype": "success"},
        ],
    )


# ---- the event shape ----


def test_the_captured_tool_result_shape_is_read(tmp_path):
    """Captured on the laptop 2026-11-24, Claude Code 2.1.331, from the launcher's argv with
    a stub stdio server named `mailbox`: the tool_result content arrives as one string of
    compact JSON, while the host sends a list of text blocks. Both are read."""
    line = (
        '{"type": "user", "message": {"role": "user", "content": [{"tool_use_id": '
        '"toolu_019V", "type": "tool_result", "content": "{\\"count\\":1,\\"messages\\":'
        '[{\\"auth\\":{\\"dkim\\":\\"fail\\",\\"dmarc\\":\\"pass\\",\\"spf\\":\\"pass\\"},'
        '\\"auto_submitted\\":null,\\"categories\\":[\\"newsletter\\"],\\"date\\":'
        '\\"2026-11-24\\",\\"has_attachment\\":false,\\"has_list_id\\":true,'
        '\\"has_list_unsubscribe\\":true,\\"keep_hit\\":false,\\"precedence\\":\\"bulk\\",'
        '\\"sender_domain\\":\\"example.org\\",\\"size\\":\\"small\\",\\"uid\\":\\"4711\\"}],'
        '\\"truncated\\":false}"}]}, "parent_tool_use_id": null}'
    )
    runs = tmp_path / "runs"
    write_run(runs, D_RUN, [tool_use("toolu_019V", since=SINCE), line])
    got = digest.collect(runs, DAY)
    assert list(got.records) == ["4711"]
    assert got.records["4711"]["auth"]["dkim"] == "fail"
    assert got.complete


def test_list_content_is_read_too(tmp_path):
    runs = tmp_path / "runs"
    write_run(runs, D_RUN, [tool_use("a", since=SINCE), tool_result("a", [rec("1")], as_list=True)])
    assert list(digest.collect(runs, DAY).records) == ["1"]


# ---- which day a mail belongs to: the run that saw it first, never its Date header ----


def test_a_mail_belongs_to_the_day_a_run_first_saw_it(tmp_path):
    runs = tmp_path / "runs"
    search_run(runs, PREV, [rec("1")], since="2026-11-21")
    search_run(runs, D_RUN, [rec("1"), rec("2")])
    search_run(runs, D_RUN2, [rec("2"), rec("3")])
    search_run(runs, NEXT, [rec("3"), rec("4")], since=DAY)
    assert sorted(digest.collect(runs, DAY).records) == ["2", "3"]


@pytest.mark.parametrize("date", ["1999-01-01", "2099-12-31", None, "garbage"])
def test_a_senders_date_header_cannot_move_a_mail_out_of_the_digest(tmp_path, date):
    """lean.py fills `date` from the Date header first, which the sender writes. A mail
    with a failed DKIM verdict and a Date of 1999 still shows on the day it was seen."""
    runs = tmp_path / "runs"
    auth = {"dkim": "fail", "dmarc": "fail", "spf": "fail"}
    search_run(runs, D_RUN, [rec("7", "spoof.example", date=date, auth=auth)])
    text = digest.render(DAY, digest.collect(runs, DAY), "2026-11-24T06:00:40Z")
    assert "| 7 | spoof.example | fail | fail | fail |" in text


@pytest.mark.parametrize(
    "events",
    [
        [tool_use("a", since=SINCE), tool_result("a", [rec("9")], is_error=True)],
        [tool_result("unknown-id", [rec("9")])],
        [tool_use("a", name="mcp__other__search"), tool_result("a", [rec("9")])],
        [tool_use("a", since=SINCE), {"type": "user", "message": {"content": "plain text"}}],
        ["not json at all"],
        [{"launcher": {"refused": "gate"}}],
        [tool_use("a", since=SINCE), tool_result("a", [{**rec("9"), "uid": "9; rm"}])],
    ],
    ids=["error", "unknown-id", "other-server", "text-turn", "not-json", "note", "bad-uid"],
)
def test_nothing_but_a_mailbox_tool_result_counts(tmp_path, events):
    runs = tmp_path / "runs"
    write_run(runs, D_RUN, events)
    assert digest.collect(runs, DAY).records == {}


def test_a_symlinked_run_file_is_not_followed(tmp_path):
    runs = tmp_path / "runs"
    real = search_run(tmp_path / "elsewhere", D_RUN, [rec("1")])
    runs.mkdir()
    (runs / D_RUN).symlink_to(real)
    assert digest.collect(runs, DAY).records == {}


def test_a_link_swapped_in_after_the_check_is_not_followed(tmp_path):
    """C9 (James, #113): collect() checks lstat, then _read_run opened by name; the owner
    could swap a link in between. _read_run itself opens O_NOFOLLOW and reads only a
    descriptor that fstat says is a regular file."""
    real = search_run(tmp_path / "elsewhere", D_RUN, [rec("1")])
    runs = tmp_path / "runs"
    runs.mkdir()
    (runs / D_RUN).symlink_to(real)
    assert digest._read_run(runs / D_RUN) is None


def test_a_fifo_swapped_in_does_not_hang_the_launcher(tmp_path):
    runs = tmp_path / "runs"
    runs.mkdir()
    os.mkfifo(runs / D_RUN)
    got = []
    t = threading.Thread(target=lambda: got.append(digest._read_run(runs / D_RUN)), daemon=True)
    t.start()
    t.join(5)
    assert not t.is_alive() and got == [None]


# ---- coverage: when may the digest say an exact number ----


@pytest.mark.parametrize(
    ("since", "kwargs", "complete"),
    [
        (SINCE, {}, True),
        ("2026-11-01", {}, False),  # a since the launcher did not give: the bot strayed
        (SINCE, {"truncated": True}, False),
        (DAY, {}, False),  # not the launcher's day either
        (SINCE, {"unseen": True}, False),  # a filtered search does not see every mail
        (SINCE, {"flagged": False}, False),
        ("23.11.2026", {}, False),
    ],
    ids=["plain", "stray-since", "truncated", "late-since", "unseen", "flagged", "bad-since"],
)
def test_coverage(tmp_path, since, kwargs, complete):
    runs = tmp_path / "runs"
    search_run(runs, PREV, [], since="2026-11-21")
    search_run(runs, D_RUN, [rec("1")], since=since, **kwargs)
    assert digest.collect(runs, DAY).complete is complete


def test_a_gap_since_the_previous_searching_run_makes_the_day_incomplete(tmp_path):
    runs = tmp_path / "runs"
    search_run(runs, "run-20261120T190000Z.jsonl", [rec("1")], since="2026-11-19")
    search_run(runs, D_RUN, [rec("2")])
    assert not digest.collect(runs, DAY).complete


def test_only_the_launchers_search_counts_records(tmp_path):
    """The bot picks its arguments. A read of an old UID, or a search from an earlier day,
    would make old mail look new, so neither contributes a record or coverage."""
    runs = tmp_path / "runs"
    write_run(
        runs,
        D_RUN,
        [
            tool_use("s", since=SINCE),
            tool_result("s", [rec("50")]),
            tool_use("r", name="mcp__mailbox__read", uid="3"),
            tool_result("r", [rec("3")]),
            tool_use("o", since="2020-01-01"),
            tool_result("o", [rec("4"), rec("5")]),
        ],
    )
    got = digest.collect(runs, DAY)
    assert sorted(got.records) == ["50"]
    assert got.complete  # the launcher's search was whole; the strays add nothing


def test_a_stray_search_alone_gives_at_least(tmp_path):
    runs = tmp_path / "runs"
    search_run(runs, D_RUN, [rec("4")], since="2020-01-01")
    got = digest.collect(runs, DAY)
    assert got.records == {} and not got.complete


@pytest.mark.parametrize(("low", "complete"), [(101, True), (120, True), (121, False)])
def test_a_truncated_search_that_overlaps_the_previous_run_is_whole(tmp_path, low, complete):
    """search returns the newest records by UID. If its lowest UID is at or below the
    highest the previous searching run saw, nothing between the two runs was skipped."""
    runs = tmp_path / "runs"
    search_run(runs, D_RUN, [rec(str(u)) for u in range(120, 100, -1)])
    search_run(runs, D_RUN2, [rec(str(u)) for u in range(low + 99, low - 1, -1)], truncated=True)
    assert digest.collect(runs, DAY).complete is complete


def test_one_incomplete_run_of_the_day_makes_the_day_incomplete(tmp_path):
    runs = tmp_path / "runs"
    search_run(runs, D_RUN, [rec("1")])
    search_run(runs, D_RUN2, [rec("2")], truncated=True)
    assert not digest.collect(runs, DAY).complete


def test_a_run_that_never_searched_is_no_gap_and_no_coverage(tmp_path):
    runs = tmp_path / "runs"
    search_run(runs, D_RUN, [rec("1")])
    write_run(runs, D_RUN2, [{"launcher": {"refused": "gate", "reason": "x"}}])
    assert digest.collect(runs, DAY).complete
    assert not digest.collect(tmp_path / "runs", "2026-11-25").complete  # no run that day


def test_a_truncated_search_is_covered_by_all_four_size_buckets_of_the_same_run(tmp_path):
    runs = tmp_path / "runs"
    events = [tool_use("t", since=SINCE), tool_result("t", [rec("1")], truncated=True)]
    for i, bucket in enumerate(("small", "medium", "large", "huge")):
        events += [tool_use(f"b{i}", since=SINCE, size=bucket), tool_result(f"b{i}", [rec("1")])]
    write_run(runs, D_RUN, events)
    assert digest.collect(runs, DAY).complete
    write_run(runs, D_RUN, events[:-2])  # three buckets only
    assert not digest.collect(runs, DAY).complete


# ---- the text ----


def test_the_digest_counts_what_the_owner_asked_for(tmp_path):
    runs = tmp_path / "runs"
    records = (
        [rec(str(i), "big.example") for i in range(1, 6)]
        + [rec(str(i), "news.example", unsub=True) for i in range(6, 9)]
        + [rec("9", "bank.example", auth={"dkim": "fail", "dmarc": "pass", "spf": "pass"})]
        + [rec("10", "shop.example", auth={"dkim": "pass", "dmarc": "pass", "spf": "softfail"})]
        + [rec("11", "odd.example", auth={"dkim": "none", "dmarc": "permerror", "spf": "pass"})]
        + [rec("12", "fine.example", auth={"dkim": "neutral", "dmarc": "none", "spf": "none"})]
    )
    search_run(runs, D_RUN, records)
    text = digest.render(DAY, digest.collect(runs, DAY), "2026-11-24T06:00:40Z")
    assert "# Mailbox digest, 2026-11-23" in text
    assert re.search(r"^New mails: 12$", text, re.M)
    rows = re.findall(r"^\| (\d+) \| ([a-z.]+) \| (\d+) \|$", text, re.M)
    assert rows[:2] == [("1", "big.example", "5"), ("2", "news.example", "3")]
    assert [r[1] for r in rows[2:7]] == sorted(r[1] for r in rows[2:7])  # ties A to Z
    assert "news.example (3)" in text
    failed = re.findall(r"^\| (\d+) \| ([a-z.]+) \| (\w+) \| (\w+) \| (\w+) \|$", text, re.M)
    assert [(f[0], f[1]) for f in failed] == [
        ("9", "bank.example"),
        ("10", "shop.example"),
        ("11", "odd.example"),
    ]
    assert "fine.example" not in "".join(str(f) for f in failed)
    assert "at least" not in text


def test_an_incomplete_day_says_at_least(tmp_path):
    runs = tmp_path / "runs"
    search_run(runs, D_RUN, [rec("1")], truncated=True)
    text = digest.render(DAY, digest.collect(runs, DAY), "2026-11-24T06:00:40Z")
    assert re.search(r"^New mails: at least 1$", text, re.M)
    assert "lower bounds" in text


def test_a_day_no_run_saw_says_so(tmp_path):
    text = digest.render(DAY, digest.collect(tmp_path / "none", DAY), "2026-11-24T06:00:40Z")
    assert re.search(r"^New mails: at least 0$", text, re.M)


@pytest.mark.parametrize(
    "domain", ["a|b.example", "x.example\n# injected", "Upper.Example", None, "invalid", 7]
)
def test_a_value_that_is_no_domain_never_reaches_the_file(tmp_path, domain):
    runs = tmp_path / "runs"
    search_run(runs, D_RUN, [rec("1", domain=domain)])
    text = digest.render(DAY, digest.collect(runs, DAY), "2026-11-24T06:00:40Z")
    assert "(no domain)" in text
    assert "injected" not in text and "|b" not in text and "Upper" not in text


def test_an_unknown_verdict_word_is_not_echoed(tmp_path):
    runs = tmp_path / "runs"
    auth = {"dkim": "fail", "dmarc": "IGNORE ALL", "spf": "pass"}
    search_run(runs, D_RUN, [rec("1", auth=auth)])
    text = digest.render(DAY, digest.collect(runs, DAY), "2026-11-24T06:00:40Z")
    assert "IGNORE" not in text and "| 1 | example.org | fail | ? | pass |" in text


# ---- the file ----


def test_the_digest_file_is_owner_only_and_replaced_whole(tmp_path):
    digests = tmp_path / "digests"
    digests.mkdir(mode=0o700)
    path = digest.write(digests, DAY, "first\n")
    assert path.name == "digest-2026-11-23.md"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    digest.write(digests, DAY, "second\n")
    assert path.read_text() == "second\n"
    assert sorted(p.name for p in digests.iterdir()) == ["digest-2026-11-23.md"]


def test_a_planted_symlink_is_replaced_not_followed(tmp_path):
    digests = tmp_path / "digests"
    digests.mkdir()
    victim = tmp_path / "victim"
    victim.write_text("keep")
    (digests / f"digest-{DAY}.md").symlink_to(victim)
    digest.write(digests, DAY, "digest\n")
    assert victim.read_text() == "keep"
    assert not (digests / f"digest-{DAY}.md").is_symlink()


def test_build_writes_yesterdays_digest(tmp_path):
    runs, digests = tmp_path / "runs", tmp_path / "digests"
    digests.mkdir()
    search_run(runs, D_RUN, [rec("1"), rec("2")])
    result = digest.build(DAY, runs, digests, "2026-11-24T06:00:40Z")
    assert result == {"day": DAY, "mails": 2, "complete": True}
    assert "New mails: 2" in (digests / f"digest-{DAY}.md").read_text()


def test_digest_imports_nothing_from_the_package():
    source = (LAUNCHER / "digest.py").read_text()
    assert not re.search(r"^\s*(from|import)\s+mailbox_cleanup", source, re.M)
    assert os.access(LAUNCHER / "digest.py", os.R_OK)
