# tests/test_no_send.py
"""The package contains no code that sends mail (spec §5).

Two scans, because there are two shipped forms:
- the source tree a plugin checkout runs (`src/` through `bin/mailbox-autopilot`);
- the built wheel (#75): what `pip install` would deliver, built here from the tree.

Scope: this package only, not its dependencies. tests/conftest.py seeds GreenMail over
SMTP and is not shipped; hooks/block_send.py names the patterns in order to block them.
"""

import re
import shutil
import subprocess
import zipfile
from pathlib import Path

ROOT = Path(__file__).parent.parent
SRC = ROOT / "src"
FORBIDDEN = [
    re.compile(r"^\s*(import|from)\s+smtplib\b", re.M),
    re.compile(r"\bsmtplib\b"),
    re.compile(r"\bSMTP(?:_SSL)?\s*\("),
    re.compile(r"\bsendmail\b"),
    re.compile(r"\bsend_message\s*\("),
    re.compile(r"\bosascript\b"),
    re.compile(r"\bsmtps?://", re.I),
]


def _offenders(name: str, text: str) -> list[str]:
    return [f"{name}: {rx.pattern}" for rx in FORBIDDEN if rx.search(text)]


def test_no_send_code_in_the_plugin_checkout():
    files = [*SRC.rglob("*.py"), *(ROOT / "bin").iterdir()]
    offenders = []
    for f in files:
        offenders += _offenders(str(f.relative_to(ROOT)), f.read_text(encoding="utf-8"))
    assert any(f.suffix == ".py" for f in files), f"no .py files under {SRC}: the guard is vacuous"
    assert offenders == [], offenders


def test_no_send_code_in_the_built_wheel(tmp_path):
    # A missing uv fails the test rather than skipping it: a skipped guard guards nothing.
    uv = shutil.which("uv")
    assert uv, "uv is needed to build the wheel (https://docs.astral.sh/uv/)"
    subprocess.run(
        [uv, "build", "--wheel", "--out-dir", str(tmp_path), str(ROOT)],
        check=True,
        capture_output=True,
    )
    (wheel,) = tmp_path.glob("*.whl")
    offenders, scanned = [], 0
    with zipfile.ZipFile(wheel) as zf:
        for name in zf.namelist():
            text = zf.read(name).decode("utf-8", errors="replace")
            scanned += name.endswith(".py")
            offenders += _offenders(name, text)
    assert scanned > 0, f"no .py files in {wheel.name}: the guard is vacuous"
    assert offenders == [], offenders
