import importlib.util
import re
import sys
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "leak_guard", Path(__file__).parent.parent / "scripts" / "leak_guard.py"
)
lg = importlib.util.module_from_spec(_SPEC)
sys.modules["leak_guard"] = lg  # dataclasses need the module registered
_SPEC.loader.exec_module(lg)


def _scan(text, private=(), allow=(), path="a.py"):
    entries = [(path, n, line) for n, line in enumerate(text.splitlines(), start=1)]
    return lg.scan_lines(entries, list(private), list(allow))


def test_example_domains_and_non_addresses_pass():
    text = (
        "x = 'anna@example.com'\n"
        "y = 'bot@example.org'\n"
        "z = 'test@localhost'\n"
        "w = '<a@x.example>'\n"
        "- uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1\n"
        "t = 'u@x'\n"
        "/plugin install mailbox-autopilot@neckarshore-ai\n"
    )
    assert _scan(text) == []


def test_real_looking_address_hits():
    hits = _scan("x = 'jane.doe@some-company.test'\n")
    assert [(h.line, h.kind, h.label) for h in hits] == [(1, "public", "email")]


def test_valid_iban_hits_invalid_does_not():
    hits = _scan("ok DE89 3704 0044 0532 0130 00\nnot DE00 1234 5678 9012 3456 78\n")
    assert [(h.line, h.label) for h in hits] == [(1, "iban")]


def test_international_phone_hits():
    hits = _scan("call +49 711 1234567 now\nversion 1.2.3456789\n")
    assert [(h.line, h.label) for h in hits] == [(1, "phone")]


def test_private_hit_never_reveals_pattern_or_text(capsys):
    hits = _scan("hello Zwergenhausen\n", private=[r"zwergenhausen"], path="a.md")
    assert [(h.kind, h.label) for h in hits] == [("private", "private-list hit")]
    lg.report(hits)
    out = capsys.readouterr().out
    assert "a.md:1: private-list hit" in out
    assert "wergenhausen" not in out.lower()


def test_allowlist_exempts_with_reason():
    allow = lg.parse_allow("pyproject.toml:^authors = # package metadata author line\n")
    line = 'authors = [{name = "X", email = "x@owner.test"}]\n'
    assert _scan(line, allow=allow, path="pyproject.toml") == []


def test_allowlist_entry_without_reason_is_rejected():
    with pytest.raises(ValueError):
        lg.parse_allow("pyproject.toml:^authors = \n")


def test_added_lines_parses_unified_zero_context_diff():
    diff = (
        "diff --git a/n.md b/n.md\n"
        "--- a/n.md\n"
        "+++ b/n.md\n"
        "@@ -3,0 +4,2 @@\n"
        "+first\n"
        "+second\n"
        "diff --git a/gone.md b/gone.md\n"
        "--- a/gone.md\n"
        "+++ /dev/null\n"
        "@@ -1 +0,0 @@\n"
        "-removed\n"
    )
    assert lg.added_lines(diff) == [("n.md", 4, "first"), ("n.md", 5, "second")]


def test_scan_files_skips_binary(tmp_path):
    p = tmp_path / "img.png"
    p.write_bytes(b"\x89PNG\x00\x00jane@some-company.test")
    assert lg.scan_files([p], [], []) == []


# --- Fix round 1: fail-open findings from review -------------------------------


def test_quoted_non_ascii_path_is_unquoted_and_scanned():
    # core.quotepath default: git writes non-ASCII names as a quoted C-style string
    # with octal byte escapes ("t\303\244st.md" == "täst.md" UTF-8-encoded).
    diff = (
        'diff --git "a/t\\303\\244st.md" "b/t\\303\\244st.md"\n'
        '--- "a/t\\303\\244st.md"\n'
        '+++ "b/t\\303\\244st.md"\n'
        "@@ -0,0 +1 @@\n"
        "+hello\n"
    )
    assert lg.added_lines(diff) == [("täst.md", 1, "hello")]


@pytest.mark.parametrize(
    "header, expected",
    [
        ('"a/my d\\303\\244/f.md" "b/my d\\303\\244/f.md"', "my dä/f.md"),
        ("a/my file.md b/my file.md", "my file.md"),
    ],
    ids=["quoted-with-space", "unquoted-with-space"],
)
def test_a_path_with_a_space_ends_its_header_with_a_tab(header, expected):
    """git ends a ---/+++ header with a tab when the path contains a space. Read as
    part of the path, the tab made the guard refuse every such file as an unrecognized
    target (measured with a real `git diff` while fixing #87)."""
    a, b = (s + "\t" for s in _split_header(header))
    diff = f"diff --git {header}\n--- {a}\n+++ {b}\n@@ -0,0 +1 @@\n+hello\n"
    assert lg.added_lines(diff) == [(expected, 1, "hello")]


def _split_header(header):
    if header.startswith('"'):
        a, _, b = header[1:].partition('" "')
        return f'"{a}"', f'"{b}'
    half = len(header) // 2
    return header[:half], header[half + 1 :]


def test_unrecognized_diff_target_fails_closed():
    diff = "diff --git a/x.md b/x.md\n--- a/x.md\n+++ garbage\n@@ -0,0 +1 @@\n+hello\n"
    with pytest.raises(lg.LeakGuardError):
        lg.added_lines(diff)


def test_added_content_line_starting_with_plusplusplus_is_not_read_as_a_header():
    # An added line whose TEXT is "++ harmless" becomes, under a '+' diff prefix,
    # the raw diff line "+++ harmless" -- indistinguishable from a file header by
    # prefix alone. Only the hunk's declared line count may end the hunk.
    diff = (
        "diff --git a/x.md b/x.md\n"
        "--- a/x.md\n"
        "+++ b/x.md\n"
        "@@ -1,0 +2,2 @@\n"
        "+++ harmless\n"
        "+jane.doe@some-company.test\n"
    )
    assert lg.added_lines(diff) == [
        ("x.md", 2, "++ harmless"),
        ("x.md", 3, "jane.doe@some-company.test"),
    ]


def test_binary_hit_paths_extracts_new_side_path():
    diff = (
        "diff --git a/note.md b/note.md\n"
        "index 3b18e51..2c95405 100644\n"
        "Binary files a/note.md and b/note.md differ\n"
    )
    assert lg.binary_hit_paths(diff) == ["note.md"]


def test_binary_hit_paths_ignores_pure_deletions():
    diff = (
        "diff --git a/note.md b/note.md\n"
        "index 3b18e51..0000000 100644\n"
        "Binary files a/note.md and /dev/null differ\n"
    )
    assert lg.binary_hit_paths(diff) == []


@pytest.mark.parametrize(
    "header",
    [
        '"a/my d\\303\\244/img.png" "b/my d\\303\\244/img.png"',
        'a/old.png "b/my d\\303\\244/img.png"',
    ],
    ids=["quoted-with-space", "rename-onto-quoted-with-space"],
)
def test_binary_hit_paths_unquotes_a_quoted_path_with_a_space(header):
    """git quotes a path only for a non-ASCII or control byte; with a space as well,
    the greedy header split kept the closing quote and the path was wrong (James, #87)."""
    diff = f"diff --git {header}\nindex 3b18e51..2c95405 100644\nGIT binary patch\nliteral 12\n"
    assert lg.binary_hit_paths(diff) == ["my dä/img.png"]


def test_diff_new_paths_unquotes_a_quoted_path_with_a_space():
    diff = (
        'diff --git "a/my d\\303\\244/f.md" "b/my d\\303\\244/f.md"\n'
        "new file mode 100644\nindex 0000000..e69de29\n"
    )
    assert lg.diff_new_paths(diff) == ["my dä/f.md"]


def test_binary_hit_paths_extracts_path_for_git_binary_patch_block():
    diff = (
        "diff --git a/img.png b/img.png\n"
        "index 3b18e51..2c95405 100644\n"
        "GIT binary patch\n"
        "literal 12\n"
        "Qc$@)?000\n"
    )
    assert lg.binary_hit_paths(diff) == ["img.png"]


def test_path_allowed_checks_path_only_not_content():
    allow = lg.parse_allow("assets/logo.png:. # design asset, reviewed by hand\n")
    assert lg.path_allowed("assets/logo.png", allow) is True
    assert lg.path_allowed("other/logo.png", allow) is False


def test_private_list_entries_are_stripped_of_whitespace():
    raw = "  zwergenhausen  \r\nfoo\r\n\n   \n"
    assert lg._parse_private_list(raw) == ["zwergenhausen", "foo"]


def test_allowlist_entry_with_empty_regex_is_rejected():
    with pytest.raises(ValueError):
        lg.parse_allow("x.md: # r\n")


# --- Fix round 2: hunk-count desync from embedded line terminators ------------


def test_embedded_form_feed_inside_added_line_is_not_split():
    # str.splitlines() treats \x0c (form feed) as a line break; git's diff format
    # does not -- only '\n' ends a diff-stream line. A naive splitlines() parse
    # desyncs the hunk's declared line count and drops the LAST added line
    # ("more ...") outside the hunk entirely, where it goes unscanned.
    diff = (
        "diff --git a/a.md b/a.md\n"
        "index df967b9..9f7498a 100644\n"
        "--- a/a.md\n"
        "+++ b/a.md\n"
        "@@ -1,0 +2,3 @@ base\n"
        "+x\x0cy\n"
        "+harmless\n"
        "+more jane.doe@some-company.test\n"
    )
    assert lg.added_lines(diff) == [
        ("a.md", 2, "x\x0cy"),
        ("a.md", 3, "harmless"),
        ("a.md", 4, "more jane.doe@some-company.test"),
    ]


def test_embedded_cr_inside_added_line_is_not_split():
    diff = (
        "diff --git a/a.md b/a.md\n"
        "index df967b9..683029b 100644\n"
        "--- a/a.md\n"
        "+++ b/a.md\n"
        "@@ -1,0 +2,2 @@ base\n"
        "+x\ry\n"
        "+jane.doe@some-company.test\n"
    )
    assert lg.added_lines(diff) == [
        ("a.md", 2, "x\ry"),
        ("a.md", 3, "jane.doe@some-company.test"),
    ]


def test_crlf_terminated_content_line_has_trailing_cr_stripped():
    # Measured 2026-09-24: git reproduces a CRLF-terminated source file's own line
    # ending in its diff output ("+content\r\n") -- the content's trailing \r is
    # stripped so the scanned text matches the LF case exactly.
    diff = (
        "diff --git a/a.md b/a.md\r\n"
        "--- a/a.md\r\n"
        "+++ b/a.md\r\n"
        "@@ -1 +1 @@\r\n"
        "-old\r\n"
        "+jane.doe@some-company.test\r\n"
    )
    assert lg.added_lines(diff) == [("a.md", 1, "jane.doe@some-company.test")]


def test_malformed_hunk_line_fails_closed():
    diff = (
        "diff --git a/a.md b/a.md\n"
        "--- a/a.md\n"
        "+++ b/a.md\n"
        "@@ -0,0 +1,2 @@\n"
        "+first\n"
        "garbage-no-prefix\n"
    )
    with pytest.raises(lg.LeakGuardError):
        lg.added_lines(diff)


def test_zero_count_hunk_with_added_line_fails_closed():
    # Probe p4: a hunk that declares 0 old + 0 new lines, immediately followed by
    # a '+' line. Before this fix: 0 scanned, rc=0 -- the line was silently read
    # as "outside a hunk" and ignored.
    diff = (
        "diff --git a/a.md b/a.md\n"
        "--- a/a.md\n"
        "+++ b/a.md\n"
        "@@ -1,0 +1,0 @@\n"
        "+jane.doe@some-company.test\n"
    )
    with pytest.raises(lg.LeakGuardError):
        lg.added_lines(diff)


def test_added_line_count_invariant_catches_dev_null_with_content():
    # A crafted diff that targets /dev/null (a deletion, path=None) yet still
    # carries a declared '+' line -- the sanity invariant (# of '+' lines in all
    # hunks == # of scanned entries) must catch this rather than silently drop it.
    diff = (
        "diff --git a/a.md b/a.md\n"
        "--- a/a.md\n"
        "+++ /dev/null\n"
        "@@ -0,0 +1 @@\n"
        "+jane.doe@some-company.test\n"
    )
    with pytest.raises(lg.LeakGuardError):
        lg.added_lines(diff)


def test_binary_added_line_does_not_crash_and_respects_allowlist(tmp_path, monkeypatch):
    # --text forces even genuinely-binary content into the diff; reading it as
    # strict UTF-8 must not raise -- and an allowlisted path is still exempted.
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".leak-guard-allow").write_text(
        "bin/allowed.dat:. # binary asset, reviewed by hand\n", encoding="utf-8"
    )
    diff_bytes = (
        b"diff --git a/bin/allowed.dat b/bin/allowed.dat\n"
        b"--- a/bin/allowed.dat\n"
        b"+++ b/bin/allowed.dat\n"
        b"@@ -0,0 +1 @@\n"
        b"+\x89\xff\xfegarbage\n"
    )
    diff_file = tmp_path / "pr.diff"
    diff_file.write_bytes(diff_bytes)
    rc = lg.main(["--diff", str(diff_file)])
    assert rc == 0


def test_binary_added_line_in_non_allowlisted_path_is_still_scanned(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    diff_bytes = (
        b"diff --git a/bin/other.dat b/bin/other.dat\n"
        b"--- a/bin/other.dat\n"
        b"+++ b/bin/other.dat\n"
        b"@@ -0,0 +1 @@\n"
        b"+\x89\xff\xfegarbage\n"
    )
    diff_file = tmp_path / "pr.diff"
    diff_file.write_bytes(diff_bytes)
    rc = lg.main(["--diff", str(diff_file)])
    out = capsys.readouterr().out
    assert rc in (0, 1)
    assert "1 added line(s) scanned" in out


# --- #74: reserved .invalid, vendor system addresses, local deny list, full tree -------


def test_the_reserved_invalid_domain_passes():
    assert _scan("Delivery failed for example@nowhere.invalid.\n") == []


@pytest.mark.parametrize(
    "addr",
    [
        "newsletter@linkedin.com",
        "newsletter-1@linkedin.com",
        "noreply@github.com",
        "notifications@github.com",
        "automated-1@github.com",
        "MAILER-DAEMON@ionos.de",
        "bounce-1@ionos.de",
        "noreply@ionos.de",
        "news@medium.com",
    ],
)
def test_listed_vendor_system_addresses_pass(addr):
    assert _scan(f"From: {addr}\n") == []


@pytest.mark.parametrize("addr", ["jane.doe@linkedin.com", "jane@github.com", "kim@ionos.de"])
def test_a_person_at_a_vendor_domain_still_hits(addr):
    """The list names system mailboxes, not domains: a person there is personal data."""
    hits = _scan(f"From: {addr}\n")
    assert [h.label for h in hits] == ["email"]


def _git_tree(tmp_path, files):
    import subprocess

    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    for name, text in files.items():
        p = tmp_path / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "-A"], check=True)


def test_tree_mode_scans_every_tracked_file(tmp_path, monkeypatch, capsys):
    _git_tree(tmp_path, {"docs/old.md": "jane.doe@some-company.test\n", "ok.py": "x = 1\n"})
    (tmp_path / "untracked.md").write_text("jane.doe@some-company.test\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("LEAK_GUARD_PRIVATE_LIST", raising=False)
    rc = lg.main(["--tree"])
    out = capsys.readouterr().out
    assert rc == 1
    assert "docs/old.md:1: email" in out
    assert "untracked.md" not in out  # only what git tracks is shipped
    assert "2 file(s) scanned" in out


def test_tree_mode_with_a_clean_tree_passes(tmp_path, monkeypatch, capsys):
    _git_tree(tmp_path, {"a.py": "x = 'anna@example.com'\n"})
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("LEAK_GUARD_PRIVATE_LIST", raising=False)
    assert lg.main(["--tree"]) == 0
    assert "1 file(s) scanned, 0 hit(s)" in capsys.readouterr().out


def test_an_unknown_option_is_refused_not_read_as_a_file(tmp_path, monkeypatch, capsys):
    """A mistyped mode must not scan nothing and pass."""
    monkeypatch.chdir(tmp_path)
    assert lg.main(["--tre"]) == 2


def test_the_local_deny_list_is_read_and_never_printed(tmp_path, monkeypatch, capsys):
    _git_tree(tmp_path, {"notes.md": "host: mail.private-host.example\n"})
    (tmp_path / ".leakguard-local.txt").write_text(
        "# local, git-ignored\nprivate-host\n", encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("LEAK_GUARD_PRIVATE_LIST", raising=False)
    rc = lg.main(["--tree"])
    out = capsys.readouterr().out
    assert rc == 1
    assert "notes.md:1: private-list hit" in out
    assert "private-host" not in out
    assert "public patterns only" not in out


@pytest.mark.parametrize("where", [".leakguard-local.txt", "sub/.leakguard-local.txt"])
def test_a_tracked_local_deny_list_fails_the_tree(tmp_path, monkeypatch, capsys, where):
    """The list must never be committed. Until 2026-10-29 the tree scan skipped it by
    name, so a force-added list went public through a green guard (#84, 6c578c6).
    The refusal is by path, so it holds without any private list in this context."""
    _git_tree(tmp_path, {where: "private-host\n", "ok.py": "x = 1\n"})
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("LEAK_GUARD_PRIVATE_LIST", raising=False)
    rc = lg.main(["--tree"])
    out = capsys.readouterr().out
    assert rc == 1
    assert f"{where}:0: {lg.LOCAL_LIST_TRACKED}" in out
    assert "private-host" not in out  # the list's content is never printed


def _diff_adding(path_line: str, extra: str = "") -> str:
    return f"diff --git {path_line}\n{extra}"


@pytest.mark.parametrize(
    "entry",
    [
        # a new file with content
        _diff_adding(
            "a/.leakguard-local.txt b/.leakguard-local.txt",
            "new file mode 100644\nindex 0000000..1111111\n--- /dev/null\n"
            "+++ b/.leakguard-local.txt\n@@ -0,0 +1 @@\n+private-host\n",
        ),
        # an empty new file: no hunk, no +++ header, only the diff --git line
        _diff_adding(
            "a/sub/.leakguard-local.txt b/sub/.leakguard-local.txt",
            "new file mode 100644\nindex 0000000..e69de29\n",
        ),
        # a rename onto the list's name
        _diff_adding(
            "a/notes.txt b/.leakguard-local.txt",
            "similarity index 100%\nrename from notes.txt\nrename to .leakguard-local.txt\n",
        ),
        # git's quoted path form
        _diff_adding(
            '"a/d\\303\\244/.leakguard-local.txt" "b/d\\303\\244/.leakguard-local.txt"',
            "new file mode 100644\nindex 0000000..e69de29\n",
        ),
        # git's quoted path form with a space: the greedy split kept the closing quote
        _diff_adding(
            '"a/my d\\303\\244/.leakguard-local.txt" "b/my d\\303\\244/.leakguard-local.txt"',
            "new file mode 100644\nindex 0000000..e69de29\n",
        ),
        # a rename from a plain name onto a quoted path with a space
        _diff_adding(
            'a/notes.txt "b/my d\\303\\244/.leakguard-local.txt"',
            "similarity index 100%\nrename from notes.txt\n"
            'rename to "my d\\303\\244/.leakguard-local.txt"\n',
        ),
    ],
    ids=[
        "new-with-content",
        "new-empty-in-subfolder",
        "rename-onto-name",
        "quoted-path",
        "quoted-path-with-space",
        "rename-onto-quoted-path-with-space",
    ],
)
def test_a_diff_that_adds_the_local_deny_list_fails(tmp_path, monkeypatch, capsys, entry):
    diff = tmp_path / "pr.diff"
    diff.write_text(entry, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("LEAK_GUARD_PRIVATE_LIST", raising=False)
    rc = lg.main(["--diff", str(diff)])
    out = capsys.readouterr().out
    assert rc == 1
    assert lg.LOCAL_LIST_TRACKED in out
    assert "private-host" not in out


def test_a_diff_that_only_names_the_list_in_text_passes(tmp_path, monkeypatch, capsys):
    """The refusal is about the file, not about the word: docs may name the list."""
    diff = tmp_path / "pr.diff"
    diff.write_text(
        "diff --git a/README.md b/README.md\nindex 1111111..2222222 100644\n"
        "--- a/README.md\n+++ b/README.md\n@@ -1,0 +2 @@\n+Keep .leakguard-local.txt local.\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("LEAK_GUARD_PRIVATE_LIST", raising=False)
    assert lg.main(["--diff", str(diff)]) == 0


def test_a_copy_of_a_private_pattern_under_another_name_hits():
    """A pattern's own source text rarely matches the pattern (`a\\.b` does not match
    the text `a\\.b`), so a copy of the list under another name passed the regex scan."""
    hits = _scan("private-host\\.example\n", private=["private-host\\.example"], path="copy.txt")
    assert [(h.kind, h.label) for h in hits] == [("private", "private-list source")]


def test_a_copy_of_a_private_pattern_in_another_case_hits():
    """The regex layer ignores case; the source-copy layer has to as well, or a copy
    of the list in upper case passes it (James, #87 finding 2)."""
    hits = _scan("PRIVATE-HOST\\.EXAMPLE\n", private=["private-host\\.example"], path="copy.txt")
    assert [(h.kind, h.label) for h in hits] == [("private", "private-list source")]


def test_an_allowlist_entry_exempts_a_private_source_hit():
    """The allowlist names approved content as a regex, so its own line can carry a
    private pattern's source text (`.leak-guard-allow` line 6, #74). Only an explicit
    entry with a reason exempts it, line by line, as for every other hit."""
    allow = lg.parse_allow(".leak-guard-allow:^README\\.md: # approved by the owner\n")
    line = "README.md:^Jane `jane@private-host\\.example`$ # approved\n"
    assert _scan(line, private=["private-host\\.example"], path=".leak-guard-allow") != []
    assert (
        _scan(line, private=["private-host\\.example"], allow=allow, path=".leak-guard-allow") == []
    )


def test_a_pattern_that_matches_itself_is_reported_once():
    hits = _scan("private-host\n", private=["private-host"])
    assert [(h.kind, h.label) for h in hits] == [("private", "private-list hit")]


def test_a_private_source_hit_never_reveals_the_pattern(capsys):
    lg.report([lg.Hit("copy.txt", 3, "private", "private-list source")])
    out = capsys.readouterr().out
    assert out == "copy.txt:3: private-list source\n"


def test_the_local_deny_list_is_git_ignored():
    root = Path(__file__).parent.parent
    assert ".leakguard-local.txt" in (root / ".gitignore").read_text(encoding="utf-8").split()


def test_the_repository_tree_is_clean():
    """The gate itself, on this repository: every tracked file, public patterns.
    CI runs the same scan with the private list as well."""
    import subprocess

    root = Path(__file__).parent.parent
    r = subprocess.run(
        [sys.executable, str(root / "scripts" / "leak_guard.py"), "--tree"],
        cwd=root,
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin"},
    )
    assert r.returncode == 0, r.stdout
    scanned = int(re.search(r"(\d+) file\(s\) scanned", r.stdout).group(1))
    assert scanned > 100, r.stdout  # the tree, not an empty list
