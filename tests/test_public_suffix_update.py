"""The bump of the vendored Public Suffix List, by hand in a reviewed PR (James on #96). The
script must not lose the file's provenance or shrink it without a word."""

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _load():
    path = ROOT / "scripts" / "update_public_suffix.py"
    spec = importlib.util.spec_from_file_location("update_public_suffix", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


script = _load()


def _source(rules: int, *, meta: str = "// VERSION: 2026-10-31\n// COMMIT: abc\n") -> str:
    body = "".join(f"tld{i}\n" for i in range(rules))
    return f"{meta}// ===BEGIN ICANN DOMAINS===\n{body}// ===END ICANN DOMAINS===\n"


def test_a_source_without_its_version_lines_is_refused():
    """A raw file from the GitHub repository has neither line."""
    with pytest.raises(SystemExit):
        script.check(_source(2000, meta=""))


def test_a_source_that_shrank_is_refused():
    with pytest.raises(SystemExit):
        script.check(_source(10))


def test_a_full_source_passes():
    assert len(script.check(_source(2000))[1]) == 2000
