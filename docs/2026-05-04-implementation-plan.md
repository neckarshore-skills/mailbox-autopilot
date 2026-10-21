# mailbox-cleanup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a hybrid CLI + Claude Code Skill that scans, classifies, and cleans up an IONOS IMAP mailbox safely (dry-run by default, audit-logged).

**Architecture:** Python CLI with atomic subcommands consumes/emits JSON. A Claude Code Skill orchestrates discovery → preview → apply loops. Credentials live in macOS Keychain. Soft-delete to Trash by default; nothing is `EXPUNGE`d in v1.

**Tech Stack:** Python 3.11+, `click` (CLI), `imap-tools` (IMAP), `keyring` (Keychain), `requests` (HTTPS unsubscribe), `pytest` (tests), `ruff` (lint+format), `uv` (env+packaging), Docker `greenmail` (integration test IMAP server), GitHub Actions (CI).

**Resolved spec open questions:**

| § | Question | Decision |
|---|----------|----------|
| 12.1 | CLI framework | `click` |
| 12.2 | Python version | `>=3.11` |
| 12.3 | Packaging | `uv tool install` (dev: `uv sync`); fall back to `pip install -e .[dev]` |
| 12.4 | Trash folder | SPECIAL-USE `\Trash`, fallback `Papierkorb`, then `Trash` |
| 12.5 | Archive folder | SPECIAL-USE `\Archive`, fallback `Archive` |

**Phases:**

- **Phase 0** (Tasks 1-3): Foundation — project, auth, CLI skeleton
- **Phase 1** (Tasks 4-5): IMAP foundations — client wrapper, folder resolver
- **Phase 2** (Tasks 6-8): Read-only ops — classify, scan, senders
- **Phase 3** (Tasks 9-16): Destructive ops — audit, delete, move, archive, dedupe, attachments, unsubscribe, bounces
- **Phase 4** (Tasks 17-19): Production-ready — CI, smoke test, Claude Skill

Phase 2 end = working `scan` + `senders` (useful even without rest). Phase 3 end = full cleanup CLI. Phase 4 end = production-ready with Skill.

---

## Task 1: Bootstrap project

**Files:**
- Create: `pyproject.toml`
- Create: `src/mailbox_cleanup/__init__.py`
- Create: `tests/__init__.py`
- Create: `tests/test_smoke.py`
- Create: `.gitignore`
- Create: `.python-version`

- [ ] **Step 1: Create `.python-version`**

```
3.11
```

- [ ] **Step 2: Create `.gitignore`**

```
__pycache__/
*.pyc
.pytest_cache/
.ruff_cache/
.mypy_cache/
.coverage
htmlcov/
dist/
build/
*.egg-info/
.venv/
.env
```

- [ ] **Step 3: Create `pyproject.toml`**

```toml
[project]
name = "mailbox-cleanup"
version = "0.1.0"
description = "Hybrid CLI + Claude Skill to clean up an IONOS IMAP mailbox"
requires-python = ">=3.11"
authors = [{name = "German Rauhut", email = "you@example.com"}]
dependencies = [
    "click>=8.1",
    "imap-tools>=1.5",
    "keyring>=24.0",
    "requests>=2.31",
]

[project.optional-dependencies]
dev = [
    "pytest>=8.0",
    "pytest-cov>=4.1",
    "ruff>=0.4",
]

[project.scripts]
mailbox-cleanup = "mailbox_cleanup.cli:cli"

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/mailbox_cleanup"]

[tool.pytest.ini_options]
testpaths = ["tests"]
addopts = "-v"

[tool.ruff]
line-length = 100
target-version = "py311"

[tool.ruff.lint]
select = ["E", "F", "W", "I", "B", "UP"]
```

- [ ] **Step 4: Create empty package init**

`src/mailbox_cleanup/__init__.py`:

```python
__version__ = "0.1.0"
SCHEMA_VERSION = 1  # JSON-output contract version (bump on breaking change)
```

`tests/__init__.py`: empty file.

- [ ] **Step 5: Create smoke test**

`tests/test_smoke.py`:

```python
def test_import():
    import mailbox_cleanup
    assert mailbox_cleanup.__version__ == "0.1.0"
```

- [ ] **Step 6: Install with uv and run smoke test**

```bash
cd ~/Developer/projects/mailbox-cleanup
uv venv
uv sync --extra dev
uv run pytest
```

Expected: 1 passed.

If `uv` is not installed: `brew install uv` first. Fallback: `python3.11 -m venv .venv && source .venv/bin/activate && pip install -e .[dev] && pytest`.

- [ ] **Step 7: Commit**

```bash
git add .python-version .gitignore pyproject.toml src/ tests/
git commit -m "chore: bootstrap mailbox-cleanup project (Python 3.11, click, imap-tools)"
```

---

## Task 2: Auth module (Keychain set/get/delete)

**Files:**
- Create: `src/mailbox_cleanup/auth.py`
- Create: `tests/test_auth.py`

- [ ] **Step 1: Write failing tests**

`tests/test_auth.py`:

```python
from unittest.mock import patch
from mailbox_cleanup.auth import (
    SERVICE_NAME,
    set_credentials,
    get_credentials,
    delete_credentials,
    AuthMissingError,
    Credentials,
)


def test_credentials_dataclass():
    c = Credentials(email="a@b.example", password="secret", server="imap.ionos.de")
    assert c.email == "a@b.example"
    assert c.password == "secret"
    assert c.server == "imap.ionos.de"


def test_set_and_get_credentials_roundtrip():
    with patch("mailbox_cleanup.auth.keyring") as kr:
        store = {}
        kr.set_password.side_effect = lambda s, a, p: store.setdefault((s, a), p) or store.update({(s, a): p})
        kr.get_password.side_effect = lambda s, a: store.get((s, a))

        set_credentials("user@x.example", "pw123", "imap.ionos.de")

        creds = get_credentials("user@x.example")
        assert creds.email == "user@x.example"
        assert creds.password == "pw123"
        assert creds.server == "imap.ionos.de"


def test_get_credentials_missing_raises():
    with patch("mailbox_cleanup.auth.keyring") as kr:
        kr.get_password.return_value = None
        try:
            get_credentials("nobody@x.example")
        except AuthMissingError as e:
            assert "nobody@x.example" in str(e)
            return
        raise AssertionError("Expected AuthMissingError")


def test_delete_credentials():
    with patch("mailbox_cleanup.auth.keyring") as kr:
        delete_credentials("user@x.example")
        kr.delete_password.assert_any_call(SERVICE_NAME, "user@x.example")
        kr.delete_password.assert_any_call(SERVICE_NAME, "imap-server:user@x.example")
```

- [ ] **Step 2: Run tests — verify they fail**

```bash
uv run pytest tests/test_auth.py -v
```

Expected: ImportError or 4 failures.

- [ ] **Step 3: Implement `auth.py`**

`src/mailbox_cleanup/auth.py`:

```python
from dataclasses import dataclass
import keyring

SERVICE_NAME = "mailbox-cleanup"
SERVER_KEY_PREFIX = "imap-server:"


class AuthMissingError(Exception):
    """Raised when credentials are not in Keychain."""


@dataclass(frozen=True)
class Credentials:
    email: str
    password: str
    server: str


def set_credentials(email: str, password: str, server: str) -> None:
    keyring.set_password(SERVICE_NAME, email, password)
    keyring.set_password(SERVICE_NAME, f"{SERVER_KEY_PREFIX}{email}", server)


def get_credentials(email: str) -> Credentials:
    password = keyring.get_password(SERVICE_NAME, email)
    if password is None:
        raise AuthMissingError(
            f"No credentials in Keychain for {email}. Run `mailbox-cleanup auth set`."
        )
    server = keyring.get_password(SERVICE_NAME, f"{SERVER_KEY_PREFIX}{email}") or "imap.ionos.de"
    return Credentials(email=email, password=password, server=server)


def delete_credentials(email: str) -> None:
    try:
        keyring.delete_password(SERVICE_NAME, email)
    except keyring.errors.PasswordDeleteError:
        pass
    try:
        keyring.delete_password(SERVICE_NAME, f"{SERVER_KEY_PREFIX}{email}")
    except keyring.errors.PasswordDeleteError:
        pass
```

- [ ] **Step 4: Run tests — verify they pass**

```bash
uv run pytest tests/test_auth.py -v
```

Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add src/mailbox_cleanup/auth.py tests/test_auth.py
git commit -m "feat(auth): Keychain-backed credentials with set/get/delete"
```

---

## Task 3: CLI skeleton with `auth set` / `auth test`

**Files:**
- Create: `src/mailbox_cleanup/cli.py`
- Create: `tests/test_cli_auth.py`

`auth test` connects to IMAP, lists folders, returns. Real IMAP test deferred to Task 4 — for now we mock.

- [ ] **Step 1: Write failing tests**

`tests/test_cli_auth.py`:

```python
import json
from unittest.mock import patch, MagicMock
from click.testing import CliRunner
from mailbox_cleanup.cli import cli


def test_cli_help():
    runner = CliRunner()
    result = runner.invoke(cli, ["--help"])
    assert result.exit_code == 0
    assert "auth" in result.output
    assert "scan" in result.output


def test_auth_set_writes_to_keychain():
    runner = CliRunner()
    with patch("mailbox_cleanup.cli.set_credentials") as set_creds:
        result = runner.invoke(
            cli,
            ["auth", "set", "--email", "a@b.example", "--server", "imap.ionos.de"],
            input="my-password\n",
        )
        assert result.exit_code == 0
        set_creds.assert_called_once_with("a@b.example", "my-password", "imap.ionos.de")


def test_auth_test_success_json():
    runner = CliRunner()
    fake_client = MagicMock()
    # Note: MagicMock(name=...) is reserved for the mock's repr; use explicit assignment
    inbox = MagicMock(); inbox.name = "INBOX"
    sent = MagicMock(); sent.name = "Sent"
    fake_client.__enter__.return_value.folder.list.return_value = [inbox, sent]
    with patch("mailbox_cleanup.cli.get_credentials") as get_creds, \
         patch("mailbox_cleanup.cli.imap_connect", return_value=fake_client):
        get_creds.return_value = MagicMock(email="a@b.example", server="imap.ionos.de")
        result = runner.invoke(cli, ["auth", "test", "--email", "a@b.example", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["ok"] is True
        assert data["email"] == "a@b.example"
        assert "folders" in data


def test_auth_test_missing_credentials_exit_3():
    from mailbox_cleanup.auth import AuthMissingError
    runner = CliRunner()
    with patch("mailbox_cleanup.cli.get_credentials", side_effect=AuthMissingError("no creds")):
        result = runner.invoke(cli, ["auth", "test", "--email", "a@b.example", "--json"])
        assert result.exit_code == 3
        data = json.loads(result.output)
        assert data["ok"] is False
        assert data["error_code"] == "auth_missing"
```

- [ ] **Step 2: Run tests — verify they fail**

```bash
uv run pytest tests/test_cli_auth.py -v
```

Expected: ImportError on `cli`, `imap_connect`.

- [ ] **Step 3: Add `imap_connect` placeholder + CLI**

`src/mailbox_cleanup/imap_client.py` (minimal placeholder, real impl in Task 4):

```python
"""IMAP client wrapper. Real implementation in Task 4."""
from contextlib import contextmanager
from imap_tools import MailBox
from .auth import Credentials


@contextmanager
def imap_connect(creds: Credentials):
    """Context manager that yields a connected MailBox."""
    with MailBox(creds.server).login(creds.email, creds.password) as mb:
        yield mb
```

`src/mailbox_cleanup/cli.py`:

```python
import json
import sys
import click

from . import SCHEMA_VERSION
from .auth import (
    AuthMissingError,
    Credentials,
    set_credentials,
    get_credentials,
    delete_credentials,
)
from .imap_client import imap_connect


def _emit(payload: dict, json_mode: bool) -> None:
    if json_mode:
        click.echo(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        for k, v in payload.items():
            click.echo(f"{k}: {v}")


def _fail(payload: dict, exit_code: int, json_mode: bool) -> None:
    payload["ok"] = False
    _emit(payload, json_mode)
    sys.exit(exit_code)


@click.group()
@click.version_option()
def cli():
    """Triage and clean up an IONOS IMAP mailbox."""


@cli.group()
def auth():
    """Manage IONOS credentials in macOS Keychain."""


@auth.command("set")
@click.option("--email", required=True, help="IONOS email address.")
@click.option("--server", default="imap.ionos.de", show_default=True)
@click.password_option(confirmation_prompt=False, prompt="IONOS password")
def auth_set(email: str, server: str, password: str):
    """Write IONOS credentials into macOS Keychain."""
    set_credentials(email, password, server)
    click.echo(f"Credentials stored for {email} on {server}.")


@auth.command("test")
@click.option("--email", required=True)
@click.option("--json", "json_mode", is_flag=True)
def auth_test(email: str, json_mode: bool):
    """Connect to IMAP, list folders, disconnect."""
    try:
        creds = get_credentials(email)
    except AuthMissingError as e:
        _fail(
            {"error_code": "auth_missing", "message": str(e)},
            exit_code=3,
            json_mode=json_mode,
        )
        return
    try:
        with imap_connect(creds) as mb:
            folders = [f.name for f in mb.folder.list()]
    except Exception as e:
        _fail(
            {"error_code": "connection_error", "message": str(e)},
            exit_code=2,
            json_mode=json_mode,
        )
        return
    _emit(
        {
            "ok": True,
            "email": email,
            "server": creds.server,
            "folders": folders,
            "schema_version": SCHEMA_VERSION,
        },
        json_mode=json_mode,
    )


@auth.command("delete")
@click.option("--email", required=True)
def auth_delete(email: str):
    """Remove credentials from Keychain."""
    delete_credentials(email)
    click.echo(f"Credentials removed for {email}.")
```

- [ ] **Step 4: Run tests — verify they pass**

```bash
uv run pytest tests/test_cli_auth.py -v
```

Expected: 4 passed.

- [ ] **Step 5: Verify CLI is callable**

```bash
uv run mailbox-cleanup --help
uv run mailbox-cleanup auth --help
```

Expected: help text shows `set`, `test`, `delete`.

- [ ] **Step 6: Commit**

```bash
git add src/mailbox_cleanup/cli.py src/mailbox_cleanup/imap_client.py tests/test_cli_auth.py
git commit -m "feat(cli): click skeleton with auth set/test/delete subcommands"
```

---

## Task 4: Greenmail Docker fixture + IMAP client

**Files:**
- Create: `tests/conftest.py`
- Create: `tests/docker-compose.test.yml`
- Create: `tests/fixtures/eml/plain.eml`
- Create: `tests/fixtures/eml/newsletter.eml`
- Create: `tests/fixtures/eml/bounce.eml`
- Create: `tests/fixtures/eml/automated.eml`
- Modify: `src/mailbox_cleanup/imap_client.py`
- Create: `tests/test_imap_client.py`

[Greenmail](https://greenmail-mail-test.github.io/greenmail/) is a Java IMAP/SMTP server in a Docker container. Default credentials: `user:user@localhost:3993` (IMAPS).

- [ ] **Step 1: Create docker-compose for test IMAP**

`tests/docker-compose.test.yml`:

```yaml
services:
  greenmail:
    image: greenmail/standalone:2.1.0
    ports:
      - "3143:3143"   # IMAP (plain — avoids self-signed cert issues in tests)
      - "3025:3025"   # SMTP
    environment:
      GREENMAIL_OPTS: "-Dgreenmail.setup.test.imap -Dgreenmail.setup.test.smtp -Dgreenmail.users=test:test@localhost -Dgreenmail.auth.disabled -Dgreenmail.hostname=0.0.0.0 -Dgreenmail.verbose"
```

- [ ] **Step 2: Create fixture .eml files**

`tests/fixtures/eml/plain.eml`:

```
From: alice@example.com
To: test@localhost
Subject: Lunch tomorrow?
Date: Mon, 04 May 2026 10:00:00 +0000
Message-ID: <plain-1@example.com>

Want to grab lunch?
```

`tests/fixtures/eml/newsletter.eml`:

```
From: newsletter@linkedin.com
To: test@localhost
Subject: 5 new connections this week
Date: Mon, 04 May 2026 11:00:00 +0000
Message-ID: <newsletter-1@linkedin.com>
List-Unsubscribe: <https://linkedin.com/unsub?token=abc>
List-Unsubscribe-Post: List-Unsubscribe=One-Click

You have 5 new connections.
```

`tests/fixtures/eml/bounce.eml`:

```
From: MAILER-DAEMON@ionos.de
To: test@localhost
Subject: Undelivered Mail Returned to Sender
Date: Mon, 04 May 2026 12:00:00 +0000
Message-ID: <bounce-1@ionos.de>

Delivery failed for example@nowhere.invalid.
```

`tests/fixtures/eml/automated.eml`:

```
From: notifications@github.com
To: test@localhost
Subject: [repo] PR #42 was merged
Date: Mon, 04 May 2026 13:00:00 +0000
Message-ID: <automated-1@github.com>

Your pull request was merged.
```

- [ ] **Step 3: Add `conftest.py` with Greenmail fixture**

`tests/conftest.py`:

```python
import os
import socket
import subprocess
import time
from pathlib import Path

import pytest
from imap_tools import MailBoxUnencrypted

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "eml"
COMPOSE_FILE = Path(__file__).parent / "docker-compose.test.yml"
IMAP_PORT = 3143  # Greenmail plain IMAP


def _port_open(host: str, port: int, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


@pytest.fixture(scope="session")
def greenmail():
    """Start a Greenmail IMAP/SMTP server in Docker for the test session."""
    if os.environ.get("SKIP_DOCKER_TESTS"):
        pytest.skip("SKIP_DOCKER_TESTS set")

    already_up = _port_open("127.0.0.1", IMAP_PORT)
    if not already_up:
        subprocess.run(
            ["docker", "compose", "-f", str(COMPOSE_FILE), "up", "-d"],
            check=True,
        )
        # Wait for IMAP port
        for _ in range(30):
            if _port_open("127.0.0.1", IMAP_PORT):
                break
            time.sleep(1)
        else:
            raise RuntimeError("Greenmail did not become ready")

    yield {"host": "127.0.0.1", "port": IMAP_PORT, "user": "test", "password": "test", "ssl": False}

    if not already_up:
        subprocess.run(
            ["docker", "compose", "-f", str(COMPOSE_FILE), "down"],
            check=False,
        )


def _seed_mailbox(host: str, port: int, user: str, password: str, eml_files: list[Path]):
    """Append .eml files into INBOX via SMTP."""
    import smtplib
    smtp = smtplib.SMTP("127.0.0.1", 3025)
    for eml in eml_files:
        with open(eml, "rb") as f:
            data = f.read()
        smtp.sendmail("sender@example.com", [f"{user}@localhost"], data)
    smtp.quit()
    # Wait briefly for delivery
    time.sleep(0.5)


@pytest.fixture
def fresh_mailbox(greenmail):
    """Wipe INBOX, then yield (host, port, user, password, ssl)."""
    g = greenmail
    with MailBoxUnencrypted(g["host"], port=g["port"]).login(g["user"], g["password"]) as mb:
        uids = [m.uid for m in mb.fetch(mark_seen=False) if m.uid]
        if uids:
            mb.delete(uids)
    yield g


@pytest.fixture(autouse=True)
def _disable_ssl_for_tests(monkeypatch):
    """All tests run against plain IMAP — set env so imap_connect uses MailBoxUnencrypted."""
    monkeypatch.setenv("MAILBOX_CLEANUP_SSL", "0")


@pytest.fixture
def seeded_mailbox(fresh_mailbox):
    """Seed INBOX with all fixture .eml files."""
    g = fresh_mailbox
    eml_files = sorted(FIXTURE_DIR.glob("*.eml"))
    _seed_mailbox(g["host"], g["port"], g["user"], g["password"], eml_files)
    yield g
```

- [ ] **Step 4: Update `imap_client.py` for parameterized server**

`src/mailbox_cleanup/imap_client.py`:

```python
import os
import time
from contextlib import contextmanager

from imap_tools import MailBox, MailBoxUnencrypted

from .auth import Credentials


SSL_ENV = "MAILBOX_CLEANUP_SSL"  # set to "0" to disable SSL (used by Greenmail tests)


class IMAPConnectionError(Exception):
    pass


@contextmanager
def imap_connect(
    creds: Credentials,
    *,
    port: int = 993,
    ssl: bool | None = None,
    max_retries: int = 2,
):
    """Connect to IMAP with single retry on transient failures.

    Production: ssl=True (port 993, IMAPS). Tests: env MAILBOX_CLEANUP_SSL=0 → plain.
    Backoff: 2s, 4s. After max_retries exhausted, raises IMAPConnectionError.
    """
    if ssl is None:
        ssl = os.environ.get(SSL_ENV, "1") != "0"
    Cls = MailBox if ssl else MailBoxUnencrypted
    for attempt in range(max_retries + 1):
        try:
            mb = Cls(creds.server, port=port).login(creds.email, creds.password)
            try:
                yield mb
            finally:
                mb.logout()
            return
        except Exception as e:
            if attempt < max_retries:
                time.sleep(2 ** (attempt + 1))
            else:
                raise IMAPConnectionError(
                    f"Failed to connect to {creds.server}:{port} as {creds.email}: {e}"
                ) from e
```

- [ ] **Step 5: Write integration test**

`tests/test_imap_client.py`:

```python
import pytest
from mailbox_cleanup.auth import Credentials
from mailbox_cleanup.imap_client import imap_connect, IMAPConnectionError


pytestmark = pytest.mark.integration


def test_connect_to_greenmail_lists_inbox(seeded_mailbox):
    g = seeded_mailbox
    creds = Credentials(email=g["user"], password=g["password"], server=g["host"])
    with imap_connect(creds, port=g["port"]) as mb:
        folders = [f.name for f in mb.folder.list()]
        assert "INBOX" in folders


def test_connect_with_wrong_password_raises(greenmail):
    creds = Credentials(email="test", password="WRONG", server=greenmail["host"])
    with pytest.raises(IMAPConnectionError):
        with imap_connect(creds, port=greenmail["port"], max_retries=0):
            pass


def test_seeded_mailbox_has_four_messages(seeded_mailbox):
    g = seeded_mailbox
    creds = Credentials(email=g["user"], password=g["password"], server=g["host"])
    with imap_connect(creds, port=g["port"]) as mb:
        msgs = list(mb.fetch())
        assert len(msgs) == 4
```

- [ ] **Step 6: Register integration marker in pyproject**

Edit `pyproject.toml` `[tool.pytest.ini_options]`:

```toml
[tool.pytest.ini_options]
testpaths = ["tests"]
addopts = "-v"
markers = [
    "integration: tests that require Docker (Greenmail)",
]
```

- [ ] **Step 7: Run integration tests**

```bash
uv run pytest tests/test_imap_client.py -v
```

Expected: 3 passed (Docker pulls Greenmail image first time, may take ~60s).

If Docker is unavailable: `SKIP_DOCKER_TESTS=1 uv run pytest` skips them.

- [ ] **Step 8: Commit**

```bash
git add tests/conftest.py tests/docker-compose.test.yml tests/fixtures/ \
        src/mailbox_cleanup/imap_client.py tests/test_imap_client.py pyproject.toml
git commit -m "feat(imap): connection wrapper with retry; Greenmail Docker fixture for integration tests"
```

---

## Task 5: Folder resolver (SPECIAL-USE detection)

**Files:**
- Create: `src/mailbox_cleanup/folders.py`
- Create: `tests/test_folders.py`

Resolves logical names ("trash", "archive") to actual folder paths via RFC 6154 SPECIAL-USE flags, with literal fallbacks.

- [ ] **Step 1: Write failing tests**

`tests/test_folders.py`:

```python
from unittest.mock import MagicMock
from mailbox_cleanup.folders import resolve_folder, TRASH_FALLBACKS, ARCHIVE_FALLBACKS


def _make_folder(name: str, flags=()):
    f = MagicMock()
    f.name = name
    f.flags = flags
    return f


def test_resolve_trash_via_special_use():
    folders = [_make_folder("INBOX"), _make_folder("Müll", flags=("\\Trash",))]
    mb = MagicMock()
    mb.folder.list.return_value = folders
    assert resolve_folder(mb, "trash") == "Müll"


def test_resolve_trash_via_fallback_papierkorb():
    folders = [_make_folder("INBOX"), _make_folder("Papierkorb")]
    mb = MagicMock()
    mb.folder.list.return_value = folders
    assert resolve_folder(mb, "trash") == "Papierkorb"


def test_resolve_trash_via_fallback_trash():
    folders = [_make_folder("INBOX"), _make_folder("Trash")]
    mb = MagicMock()
    mb.folder.list.return_value = folders
    assert resolve_folder(mb, "trash") == "Trash"


def test_resolve_archive_via_special_use():
    folders = [_make_folder("INBOX"), _make_folder("Backup", flags=("\\Archive",))]
    mb = MagicMock()
    mb.folder.list.return_value = folders
    assert resolve_folder(mb, "archive") == "Backup"


def test_resolve_archive_fallback():
    folders = [_make_folder("INBOX"), _make_folder("Archive")]
    mb = MagicMock()
    mb.folder.list.return_value = folders
    assert resolve_folder(mb, "archive") == "Archive"


def test_resolve_unknown_returns_none():
    folders = [_make_folder("INBOX")]
    mb = MagicMock()
    mb.folder.list.return_value = folders
    assert resolve_folder(mb, "trash") is None
```

- [ ] **Step 2: Run tests — verify they fail**

```bash
uv run pytest tests/test_folders.py -v
```

Expected: ImportError.

- [ ] **Step 3: Implement `folders.py`**

`src/mailbox_cleanup/folders.py`:

```python
"""Resolve logical folder names (trash, archive) to actual IMAP folder paths.

RFC 6154 SPECIAL-USE flags are preferred. Falls back to literal names per IONOS
German default UI.
"""

TRASH_FALLBACKS = ("Papierkorb", "Trash", "Deleted Messages", "Deleted Items")
ARCHIVE_FALLBACKS = ("Archive", "Archiv")

_SPECIAL_USE_FLAGS = {
    "trash": "\\Trash",
    "archive": "\\Archive",
    "sent": "\\Sent",
    "drafts": "\\Drafts",
    "junk": "\\Junk",
}

_FALLBACKS = {
    "trash": TRASH_FALLBACKS,
    "archive": ARCHIVE_FALLBACKS,
}


def resolve_folder(mailbox, kind: str) -> str | None:
    """Return the actual folder path for a logical kind, or None if not found.

    Looks first for a folder with the SPECIAL-USE flag matching `kind`,
    then for a folder whose name matches one of the literal fallbacks.
    """
    folders = list(mailbox.folder.list())
    target_flag = _SPECIAL_USE_FLAGS.get(kind)
    if target_flag:
        for f in folders:
            if target_flag in (f.flags or ()):
                return f.name
    for fallback in _FALLBACKS.get(kind, ()):
        for f in folders:
            if f.name == fallback:
                return f.name
    return None
```

- [ ] **Step 4: Run tests — verify they pass**

```bash
uv run pytest tests/test_folders.py -v
```

Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
git add src/mailbox_cleanup/folders.py tests/test_folders.py
git commit -m "feat(folders): resolve trash/archive via SPECIAL-USE with literal fallbacks"
```

---

## Task 6: Classification rules

**Files:**
- Create: `src/mailbox_cleanup/classify.py`
- Create: `tests/test_classify.py`

Pure functions over message metadata. No I/O. Used by `scan` and downstream operations.

- [ ] **Step 1: Write failing tests**

`tests/test_classify.py`:

```python
from mailbox_cleanup.classify import (
    is_newsletter,
    is_automated,
    is_bounce,
    classify,
    Category,
)


def test_newsletter_via_unsubscribe_header():
    headers = {"list-unsubscribe": "<https://x.com/unsub>"}
    assert is_newsletter(from_addr="x@y.example", subject="hi", headers=headers)


def test_newsletter_via_sender_pattern():
    assert is_newsletter(from_addr="newsletter@linkedin.com", subject="x", headers={})
    assert is_newsletter(from_addr="noreply@github.com", subject="x", headers={})
    assert is_newsletter(from_addr="no-reply@x.example", subject="x", headers={})
    assert is_newsletter(from_addr="news@medium.com", subject="x", headers={})
    assert is_newsletter(from_addr="marketing@x.example", subject="x", headers={})


def test_not_newsletter_for_personal():
    assert not is_newsletter(from_addr="alice@example.com", subject="hi", headers={})


def test_automated_sender_patterns():
    for local in ["notifications", "bot", "service", "alerts", "system", "daemon", "automation"]:
        assert is_automated(from_addr=f"{local}@x.example", subject="x", headers={})


def test_bounce_via_mailer_daemon():
    assert is_bounce(from_addr="MAILER-DAEMON@ionos.de", subject="x", headers={})
    assert is_bounce(from_addr="postmaster@x.example", subject="x", headers={})


def test_bounce_via_subject_prefix():
    for prefix in [
        "Undelivered Mail",
        "Returned mail: foo",
        "Mail Delivery Failure",
        "Delivery Status Notification (Failure)",
        "Auto-Reply: Out of office",
        "Out of Office: vacation",
        "Abwesenheitsnotiz",
    ]:
        assert is_bounce(from_addr="x@y.example", subject=prefix, headers={})


def test_classify_returns_set_of_categories():
    headers = {"list-unsubscribe": "<https://x>"}
    cats = classify(
        from_addr="newsletter@linkedin.com",
        subject="weekly digest",
        headers=headers,
        size_bytes=15_000_000,
    )
    assert Category.NEWSLETTER in cats
    assert Category.LARGE_ATTACHMENT in cats


def test_classify_plain_message_has_no_category():
    cats = classify(
        from_addr="alice@example.com",
        subject="lunch",
        headers={},
        size_bytes=5000,
    )
    assert cats == set()
```

- [ ] **Step 2: Run tests — verify they fail**

```bash
uv run pytest tests/test_classify.py -v
```

Expected: ImportError.

- [ ] **Step 3: Implement `classify.py`**

`src/mailbox_cleanup/classify.py`:

```python
"""Classification rules — pure functions over message metadata."""

from collections.abc import Mapping
from enum import StrEnum

LARGE_ATTACHMENT_BYTES = 10 * 1024 * 1024  # 10 MB

NEWSLETTER_LOCAL_PARTS = {"newsletter", "noreply", "no-reply", "news", "marketing"}
AUTOMATED_LOCAL_PARTS = {
    "notifications", "notification", "bot", "service", "alerts",
    "system", "daemon", "automation",
}
BOUNCE_SENDER_LOCAL_PARTS = {"mailer-daemon", "postmaster"}
BOUNCE_SUBJECT_PREFIXES = (
    "undelivered",
    "returned mail",
    "mail delivery",
    "delivery status notification",
    "auto-reply",
    "out of office",
    "abwesenheits",
)


class Category(StrEnum):
    NEWSLETTER = "newsletter"
    AUTOMATED = "automated"
    BOUNCE = "bounce"
    LARGE_ATTACHMENT = "large_attachment"


def _local_part(addr: str) -> str:
    return addr.split("@", 1)[0].lower().strip("<>")


def _has_unsubscribe(headers: Mapping[str, str]) -> bool:
    return any(k.lower() == "list-unsubscribe" for k in headers.keys())


def is_newsletter(*, from_addr: str, subject: str, headers: Mapping[str, str]) -> bool:
    if _has_unsubscribe(headers):
        return True
    return _local_part(from_addr) in NEWSLETTER_LOCAL_PARTS


def is_automated(*, from_addr: str, subject: str, headers: Mapping[str, str]) -> bool:
    return _local_part(from_addr) in AUTOMATED_LOCAL_PARTS


def is_bounce(*, from_addr: str, subject: str, headers: Mapping[str, str]) -> bool:
    if _local_part(from_addr) in BOUNCE_SENDER_LOCAL_PARTS:
        return True
    s = subject.lower().lstrip()
    return any(s.startswith(p) for p in BOUNCE_SUBJECT_PREFIXES)


def is_large_attachment(*, size_bytes: int) -> bool:
    return size_bytes > LARGE_ATTACHMENT_BYTES


def classify(
    *,
    from_addr: str,
    subject: str,
    headers: Mapping[str, str],
    size_bytes: int,
) -> set[Category]:
    """Return all categories that apply to the message."""
    cats: set[Category] = set()
    if is_newsletter(from_addr=from_addr, subject=subject, headers=headers):
        cats.add(Category.NEWSLETTER)
    if is_automated(from_addr=from_addr, subject=subject, headers=headers):
        cats.add(Category.AUTOMATED)
    if is_bounce(from_addr=from_addr, subject=subject, headers=headers):
        cats.add(Category.BOUNCE)
    if is_large_attachment(size_bytes=size_bytes):
        cats.add(Category.LARGE_ATTACHMENT)
    return cats
```

- [ ] **Step 4: Run tests — verify they pass**

```bash
uv run pytest tests/test_classify.py -v
```

Expected: 8 passed.

- [ ] **Step 5: Commit**

```bash
git add src/mailbox_cleanup/classify.py tests/test_classify.py
git commit -m "feat(classify): pure-function rules for newsletter/automated/bounce/large_attachment"
```

---

## Task 7: `scan` subcommand and report builder

**Files:**
- Create: `src/mailbox_cleanup/scan.py`
- Modify: `src/mailbox_cleanup/cli.py`
- Create: `tests/test_scan.py`

`scan` walks the folder, fetches headers + size only (no body), classifies, aggregates, and emits the JSON report from spec §7.

- [ ] **Step 1: Write failing tests**

`tests/test_scan.py`:

```python
import json
from datetime import datetime, timezone
from unittest.mock import MagicMock
from click.testing import CliRunner

from mailbox_cleanup.cli import cli, SCHEMA_VERSION
from mailbox_cleanup.scan import build_report


def _msg(from_addr, subject, size, msg_id, date, headers=None):
    m = MagicMock()
    m.from_ = from_addr
    m.subject = subject
    m.size = size
    m.uid = str(hash((msg_id,)))[-6:]
    m.headers = {k.lower(): (v,) for k, v in (headers or {}).items()}
    m.date = date
    return m


def test_build_report_counts_categories():
    msgs = [
        _msg("newsletter@linkedin.com", "weekly", 5000,
             "<n1@x>", datetime(2025, 1, 1, tzinfo=timezone.utc),
             headers={"list-unsubscribe": "<https://x>"}),
        _msg("newsletter@linkedin.com", "weekly", 5000,
             "<n2@x>", datetime(2025, 2, 1, tzinfo=timezone.utc),
             headers={"list-unsubscribe": "<https://x>"}),
        _msg("MAILER-DAEMON@ionos.de", "Undelivered Mail", 2000,
             "<b1@x>", datetime(2025, 3, 1, tzinfo=timezone.utc)),
        _msg("alice@example.com", "lunch", 15_000_000,
             "<plain@x>", datetime(2026, 1, 1, tzinfo=timezone.utc)),
    ]
    report = build_report(msgs, folder="INBOX", now=datetime(2026, 5, 4, tzinfo=timezone.utc))

    assert report["schema_version"] == SCHEMA_VERSION
    assert report["folder"] == "INBOX"
    assert report["total_messages"] == 4
    assert report["categories"]["newsletters"]["count"] == 2
    assert report["categories"]["bounces_and_autoreplies"]["count"] == 1
    assert report["categories"]["large_attachments"]["count"] == 1
    assert report["categories"]["large_attachments"]["size_mb"] >= 14
    assert report["categories"]["by_year"]["2025"] == 3
    assert report["categories"]["by_year"]["2026"] == 1


def test_build_report_top_senders_sorted():
    msgs = []
    for i in range(5):
        msgs.append(_msg("a@news.example", "x", 1000, f"<a{i}>", datetime(2025, 1, 1, tzinfo=timezone.utc),
                         headers={"list-unsubscribe": "<https://x>"}))
    for i in range(2):
        msgs.append(_msg("b@news.example", "x", 1000, f"<b{i}>", datetime(2025, 1, 1, tzinfo=timezone.utc),
                         headers={"list-unsubscribe": "<https://x>"}))
    report = build_report(msgs, folder="INBOX", now=datetime(2026, 5, 4, tzinfo=timezone.utc))
    senders = report["categories"]["newsletters"]["top_senders"]
    assert senders[0]["sender"] == "a@news.example"
    assert senders[0]["count"] == 5
    assert senders[1]["sender"] == "b@news.example"
    assert senders[1]["count"] == 2


def test_scan_cli_emits_json(seeded_mailbox, monkeypatch):
    g = seeded_mailbox

    from mailbox_cleanup.auth import Credentials
    from mailbox_cleanup import cli as cli_mod

    def fake_get_credentials(email):
        return Credentials(email=g["user"], password=g["password"], server=g["host"])

    monkeypatch.setattr(cli_mod, "get_credentials", fake_get_credentials)
    monkeypatch.setattr(cli_mod, "_DEFAULT_PORT", g["port"])

    runner = CliRunner()
    result = runner.invoke(cli, ["scan", "--email", "test", "--json"])
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["total_messages"] == 4
    assert data["categories"]["newsletters"]["count"] >= 1
    assert data["categories"]["bounces_and_autoreplies"]["count"] >= 1
```

- [ ] **Step 2: Run tests — verify they fail**

```bash
uv run pytest tests/test_scan.py -v
```

Expected: ImportError on `build_report`, CLI command not found.

- [ ] **Step 3: Implement `scan.py`**

`src/mailbox_cleanup/scan.py`:

```python
"""Discovery scan — produces the JSON report defined in spec §7."""

from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Iterable

from . import SCHEMA_VERSION
from .classify import (
    Category,
    LARGE_ATTACHMENT_BYTES,
    classify,
    is_newsletter,
    is_automated,
    is_bounce,
)

TOP_N = 10
SAMPLES = 5
OFFENDERS = 10


def _flatten_headers(raw_headers) -> dict[str, str]:
    out: dict[str, str] = {}
    if not raw_headers:
        return out
    for k, v in raw_headers.items():
        if isinstance(v, (tuple, list)) and v:
            out[k.lower()] = str(v[0])
        else:
            out[k.lower()] = str(v)
    return out


def _months_between(now: datetime, then: datetime) -> int:
    delta = now - then
    return delta.days // 30


def build_report(messages: Iterable, *, folder: str, now: datetime | None = None) -> dict:
    if now is None:
        now = datetime.now(timezone.utc)

    msgs = list(messages)
    total = len(msgs)
    total_size = sum(getattr(m, "size", 0) or 0 for m in msgs)

    nl_counter: Counter[str] = Counter()
    nl_unsub: dict[str, bool] = {}
    auto_counter: Counter[str] = Counter()
    bounces: list[dict] = []
    large: list[dict] = []
    large_size = 0
    msg_id_uids: dict[str, list[str]] = defaultdict(list)
    by_year: Counter[int] = Counter()
    older_12 = older_24 = older_60 = 0

    for m in msgs:
        from_addr = (m.from_ or "").strip()
        subject = m.subject or ""
        size = getattr(m, "size", 0) or 0
        headers = _flatten_headers(getattr(m, "headers", None))
        msg_date = getattr(m, "date", None)

        cats = classify(
            from_addr=from_addr,
            subject=subject,
            headers=headers,
            size_bytes=size,
        )

        if Category.NEWSLETTER in cats:
            nl_counter[from_addr] += 1
            nl_unsub[from_addr] = "list-unsubscribe" in headers
        if Category.AUTOMATED in cats:
            auto_counter[from_addr] += 1
        if Category.BOUNCE in cats and len(bounces) < SAMPLES:
            bounces.append({"uid": m.uid, "from": from_addr, "subject": subject})
        if Category.LARGE_ATTACHMENT in cats:
            large_size += size
            large.append({
                "uid": m.uid,
                "subject": subject,
                "size_mb": round(size / 1024 / 1024, 1),
                "from": from_addr,
            })

        # Duplicates by Message-ID
        msg_id = headers.get("message-id")
        if msg_id:
            msg_id_uids[msg_id].append(m.uid)

        # Age buckets
        if isinstance(msg_date, datetime):
            d = msg_date if msg_date.tzinfo else msg_date.replace(tzinfo=timezone.utc)
            by_year[d.year] += 1
            months = _months_between(now, d)
            if months >= 12: older_12 += 1
            if months >= 24: older_24 += 1
            if months >= 60: older_60 += 1

    duplicates = [
        {"message_id": mid, "uids": uids}
        for mid, uids in msg_id_uids.items()
        if len(uids) > 1
    ]
    duplicate_count = sum(len(d["uids"]) - 1 for d in duplicates)

    large_sorted = sorted(large, key=lambda x: x["size_mb"], reverse=True)[:OFFENDERS]

    report = {
        "schema_version": SCHEMA_VERSION,
        "scanned_at": now.isoformat().replace("+00:00", "Z"),
        "folder": folder,
        "total_messages": total,
        "size_total_mb": round(total_size / 1024 / 1024, 1),
        "categories": {
            "newsletters": {
                "count": sum(nl_counter.values()),
                "top_senders": [
                    {"sender": s, "count": c, "has_unsubscribe": nl_unsub.get(s, False)}
                    for s, c in nl_counter.most_common(TOP_N)
                ],
            },
            "automated_notifications": {
                "count": sum(auto_counter.values()),
                "top_senders": [
                    {"sender": s, "count": c}
                    for s, c in auto_counter.most_common(TOP_N)
                ],
            },
            "bounces_and_autoreplies": {
                "count": sum(1 for m in msgs if Category.BOUNCE in classify(
                    from_addr=(m.from_ or "").strip(),
                    subject=m.subject or "",
                    headers=_flatten_headers(getattr(m, "headers", None)),
                    size_bytes=getattr(m, "size", 0) or 0,
                )),
                "samples": bounces,
            },
            "large_attachments": {
                "count": len(large),
                "size_mb": round(large_size / 1024 / 1024, 1),
                "top_offenders": large_sorted,
            },
            "duplicates": {
                "count": duplicate_count,
                "groups": duplicates[:OFFENDERS],
            },
            "old_messages": {
                "older_than_12m": older_12,
                "older_than_24m": older_24,
                "older_than_60m": older_60,
            },
            "by_year": dict(sorted(by_year.items())),
        },
        "recommendations": _recommendations(nl_counter, nl_unsub, auto_counter, large_sorted, large_size),
    }
    return report


def _recommendations(nl_counter, nl_unsub, auto_counter, large, large_size_bytes) -> list[str]:
    recs: list[str] = []
    for sender, count in nl_counter.most_common(3):
        if nl_unsub.get(sender):
            recs.append(
                f"{count} messages from {sender} with Unsubscribe link → "
                f"'unsubscribe --sender={sender}'"
            )
    for sender, count in auto_counter.most_common(3):
        recs.append(
            f"{count} automated messages from {sender} → "
            f"'delete --sender={sender} --older-than=6m'"
        )
    if large:
        recs.append(
            f"{len(large)} attachments over 10 MB → "
            f"'attachments --size-gt=10mb --older-than=6m'"
        )
    return recs
```

- [ ] **Step 4: Wire `scan` into CLI**

Append to `src/mailbox_cleanup/cli.py`:

```python
from .scan import build_report

_DEFAULT_PORT = 993


@cli.command("scan")
@click.option("--email", required=True)
@click.option("--folder", default="INBOX", show_default=True)
@click.option("--json", "json_mode", is_flag=True)
def scan_cmd(email: str, folder: str, json_mode: bool):
    """Scan a folder, classify messages, emit a discovery report."""
    try:
        creds = get_credentials(email)
    except AuthMissingError as e:
        _fail({"error_code": "auth_missing", "message": str(e)}, 3, json_mode)
        return
    try:
        with imap_connect(creds, port=_DEFAULT_PORT) as mb:
            mb.folder.set(folder)
            messages = list(mb.fetch(headers_only=True, mark_seen=False, bulk=True))
    except Exception as e:
        _fail({"error_code": "connection_error", "message": str(e)}, 2, json_mode)
        return
    report = build_report(messages, folder=folder)
    if json_mode:
        click.echo(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        click.echo(f"Folder: {report['folder']}")
        click.echo(f"Total: {report['total_messages']} messages, {report['size_total_mb']} MB")
        for name, data in report["categories"].items():
            count = data.get("count") if isinstance(data, dict) else None
            if count is not None:
                click.echo(f"  {name}: {count}")
```

- [ ] **Step 5: Run tests — verify they pass**

```bash
uv run pytest tests/test_scan.py -v
```

Expected: 3 passed.

- [ ] **Step 6: Commit**

```bash
git add src/mailbox_cleanup/scan.py src/mailbox_cleanup/cli.py tests/test_scan.py
git commit -m "feat(scan): discovery scan with classification, JSON report per spec §7"
```

---

## Task 8: `senders` subcommand

**Files:**
- Modify: `src/mailbox_cleanup/cli.py`
- Create: `tests/test_senders.py`

Lists top-N senders by count, regardless of category. Useful when the user wants raw "who sends me the most" data.

- [ ] **Step 1: Write failing test**

`tests/test_senders.py`:

```python
import json
from click.testing import CliRunner
from mailbox_cleanup.cli import cli


def test_senders_lists_top_n(seeded_mailbox, monkeypatch):
    g = seeded_mailbox
    from mailbox_cleanup.auth import Credentials
    from mailbox_cleanup import cli as cli_mod

    monkeypatch.setattr(
        cli_mod, "get_credentials",
        lambda email: Credentials(email=g["user"], password=g["password"], server=g["host"]),
    )
    monkeypatch.setattr(cli_mod, "_DEFAULT_PORT", g["port"])

    runner = CliRunner()
    result = runner.invoke(cli, ["senders", "--email", "test", "--top", "5", "--json"])
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert "senders" in data
    assert isinstance(data["senders"], list)
    assert len(data["senders"]) <= 5
    if data["senders"]:
        assert "sender" in data["senders"][0]
        assert "count" in data["senders"][0]
```

- [ ] **Step 2: Run test — verify it fails**

```bash
uv run pytest tests/test_senders.py -v
```

Expected: "No such command 'senders'".

- [ ] **Step 3: Add `senders` to CLI**

Append to `src/mailbox_cleanup/cli.py`:

```python
from collections import Counter


@cli.command("senders")
@click.option("--email", required=True)
@click.option("--folder", default="INBOX", show_default=True)
@click.option("--top", default=50, show_default=True, type=int)
@click.option("--json", "json_mode", is_flag=True)
def senders_cmd(email: str, folder: str, top: int, json_mode: bool):
    """List the top-N senders by message count."""
    try:
        creds = get_credentials(email)
    except AuthMissingError as e:
        _fail({"error_code": "auth_missing", "message": str(e)}, 3, json_mode)
        return
    try:
        with imap_connect(creds, port=_DEFAULT_PORT) as mb:
            mb.folder.set(folder)
            counter: Counter[str] = Counter()
            for m in mb.fetch(headers_only=True, mark_seen=False, bulk=True):
                if m.from_:
                    counter[m.from_.strip()] += 1
    except Exception as e:
        _fail({"error_code": "connection_error", "message": str(e)}, 2, json_mode)
        return
    payload = {
        "schema_version": SCHEMA_VERSION,
        "folder": folder,
        "senders": [{"sender": s, "count": c} for s, c in counter.most_common(top)],
    }
    if json_mode:
        click.echo(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        for entry in payload["senders"]:
            click.echo(f"{entry['count']:6d}  {entry['sender']}")
```

- [ ] **Step 4: Run test — verify it passes**

```bash
uv run pytest tests/test_senders.py -v
```

Expected: 1 passed.

- [ ] **Step 5: Commit**

```bash
git add src/mailbox_cleanup/cli.py tests/test_senders.py
git commit -m "feat(cli): senders subcommand — top-N senders by count"
```

---

## Task 9: Audit logger

**Files:**
- Create: `src/mailbox_cleanup/audit.py`
- Create: `tests/test_audit.py`

Single `log_action(...)` function appends one JSON line to `~/.mailbox-cleanup/audit.log`. Used by every `--apply` operation.

- [ ] **Step 1: Write failing tests**

`tests/test_audit.py`:

```python
import json
import os
from pathlib import Path

import pytest
from mailbox_cleanup.audit import log_action, AUDIT_LOG_PATH_ENV


@pytest.fixture
def tmp_audit(tmp_path, monkeypatch):
    log_path = tmp_path / "audit.log"
    monkeypatch.setenv(AUDIT_LOG_PATH_ENV, str(log_path))
    yield log_path


def test_log_action_appends_jsonl(tmp_audit):
    log_action(
        subcommand="delete",
        args={"sender": "x@y.example", "older_than": "6m"},
        folder="INBOX",
        affected_uids=["1", "2", "3"],
        result="success",
    )
    log_action(
        subcommand="archive",
        args={"older_than": "12m"},
        folder="INBOX",
        affected_uids=["4"],
        result="success",
    )
    lines = tmp_audit.read_text().strip().split("\n")
    assert len(lines) == 2
    rec1 = json.loads(lines[0])
    assert rec1["subcommand"] == "delete"
    assert rec1["folder"] == "INBOX"
    assert rec1["affected_uids"] == ["1", "2", "3"]
    assert rec1["result"] == "success"
    assert "timestamp" in rec1
    rec2 = json.loads(lines[1])
    assert rec2["subcommand"] == "archive"


def test_log_action_records_failure(tmp_audit):
    log_action(
        subcommand="delete",
        args={"sender": "x@y.example"},
        folder="INBOX",
        affected_uids=[],
        result="failure",
        error="connection lost",
    )
    rec = json.loads(tmp_audit.read_text().strip())
    assert rec["result"] == "failure"
    assert rec["error"] == "connection lost"


def test_log_action_creates_parent_dir(tmp_path, monkeypatch):
    nested = tmp_path / "a" / "b" / "audit.log"
    monkeypatch.setenv(AUDIT_LOG_PATH_ENV, str(nested))
    log_action(subcommand="x", args={}, folder="INBOX", affected_uids=[], result="success")
    assert nested.exists()
```

- [ ] **Step 2: Run tests — verify they fail**

```bash
uv run pytest tests/test_audit.py -v
```

Expected: ImportError.

- [ ] **Step 3: Implement `audit.py`**

`src/mailbox_cleanup/audit.py`:

```python
"""Audit log writer. One JSON object per line in ~/.mailbox-cleanup/audit.log."""

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from collections.abc import Mapping, Sequence

AUDIT_LOG_PATH_ENV = "MAILBOX_CLEANUP_AUDIT_LOG"
DEFAULT_AUDIT_LOG = Path.home() / ".mailbox-cleanup" / "audit.log"


def _audit_path() -> Path:
    override = os.environ.get(AUDIT_LOG_PATH_ENV)
    return Path(override) if override else DEFAULT_AUDIT_LOG


def log_action(
    *,
    subcommand: str,
    args: Mapping[str, object],
    folder: str,
    affected_uids: Sequence[str],
    result: str,
    error: str | None = None,
) -> None:
    """Append one JSON-line record describing an applied action."""
    record = {
        "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "subcommand": subcommand,
        "args": dict(args),
        "folder": folder,
        "affected_uids": list(affected_uids),
        "result": result,
    }
    if error is not None:
        record["error"] = error
    path = _audit_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
```

- [ ] **Step 4: Run tests — verify they pass**

```bash
uv run pytest tests/test_audit.py -v
```

Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add src/mailbox_cleanup/audit.py tests/test_audit.py
git commit -m "feat(audit): JSONL audit log writer with env-var path override"
```

---

## Task 10: `delete` subcommand (soft-delete to Trash)

**Files:**
- Create: `src/mailbox_cleanup/operations/__init__.py`
- Create: `src/mailbox_cleanup/operations/filters.py`
- Create: `src/mailbox_cleanup/operations/delete.py`
- Modify: `src/mailbox_cleanup/cli.py`
- Create: `tests/test_filters.py`
- Create: `tests/test_delete.py`

`delete` selects matching UIDs via filters (sender / subject / older-than), then either lists them (dry-run) or moves them to the Trash folder (`--apply`).

- [ ] **Step 1: Write filter tests**

`tests/test_filters.py`:

```python
from datetime import datetime, timezone, timedelta
from mailbox_cleanup.operations.filters import parse_age, build_imap_search


def test_parse_age_days():
    assert parse_age("30d") == timedelta(days=30)


def test_parse_age_weeks():
    assert parse_age("2w") == timedelta(weeks=2)


def test_parse_age_months_approx_30d():
    assert parse_age("3m") == timedelta(days=90)


def test_parse_age_years_approx_365d():
    assert parse_age("2y") == timedelta(days=730)


def test_parse_age_invalid():
    import pytest
    with pytest.raises(ValueError):
        parse_age("foobar")


def test_build_imap_search_sender_only():
    q = build_imap_search(sender="newsletter@x.example")
    assert "FROM" in str(q)


def test_build_imap_search_combined():
    now = datetime(2026, 5, 4, tzinfo=timezone.utc)
    q = build_imap_search(
        sender="x@y.example",
        subject_contains="invoice",
        older_than="3m",
        now=now,
    )
    s = str(q)
    assert "FROM" in s
    assert "SUBJECT" in s
    assert "BEFORE" in s


def test_build_imap_search_no_filters_raises():
    import pytest
    with pytest.raises(ValueError):
        build_imap_search()
```

- [ ] **Step 2: Implement filters module**

`src/mailbox_cleanup/operations/__init__.py`: empty.

`src/mailbox_cleanup/operations/filters.py`:

```python
"""Filter parsing and IMAP search-criteria construction."""

import re
from datetime import datetime, timedelta, timezone

from imap_tools import AND


_AGE_RE = re.compile(r"^(\d+)([dwmy])$")
_AGE_DELTA = {
    "d": lambda n: timedelta(days=n),
    "w": lambda n: timedelta(weeks=n),
    "m": lambda n: timedelta(days=30 * n),
    "y": lambda n: timedelta(days=365 * n),
}


def parse_age(spec: str) -> timedelta:
    """Parse '30d' / '2w' / '3m' / '1y' into a timedelta."""
    m = _AGE_RE.match(spec.strip())
    if not m:
        raise ValueError(f"Bad --older-than spec: {spec!r}; expected NNd/w/m/y")
    n, unit = int(m.group(1)), m.group(2)
    return _AGE_DELTA[unit](n)


def build_imap_search(
    *,
    sender: str | None = None,
    subject_contains: str | None = None,
    older_than: str | None = None,
    now: datetime | None = None,
):
    """Build an imap-tools AND() search criteria from the given filters."""
    if not any([sender, subject_contains, older_than]):
        raise ValueError("At least one filter (sender, subject_contains, older_than) required")
    kwargs: dict = {}
    if sender:
        kwargs["from_"] = sender
    if subject_contains:
        kwargs["subject"] = subject_contains
    if older_than:
        if now is None:
            now = datetime.now(timezone.utc)
        cutoff = (now - parse_age(older_than)).date()
        kwargs["date_lt"] = cutoff
    return AND(**kwargs)
```

- [ ] **Step 3: Run filter tests**

```bash
uv run pytest tests/test_filters.py -v
```

Expected: 8 passed.

- [ ] **Step 4: Implement delete operation**

`src/mailbox_cleanup/operations/delete.py`:

```python
"""Delete operation — soft-delete (move to Trash) with dry-run by default."""

from dataclasses import dataclass

from ..folders import resolve_folder
from .filters import build_imap_search


@dataclass
class DeleteResult:
    affected_uids: list[str]
    dry_run: bool
    target_folder: str | None
    folder: str
    sample: list[dict]


def run_delete(
    mb,
    *,
    folder: str = "INBOX",
    sender: str | None = None,
    subject_contains: str | None = None,
    older_than: str | None = None,
    apply: bool = False,
    limit: int | None = None,
) -> DeleteResult:
    """Find matching messages, move to Trash if apply=True. Otherwise dry-run."""
    mb.folder.set(folder)
    criteria = build_imap_search(
        sender=sender,
        subject_contains=subject_contains,
        older_than=older_than,
    )
    msgs = list(mb.fetch(criteria, headers_only=True, mark_seen=False, limit=limit, bulk=True))
    uids = [m.uid for m in msgs if m.uid]
    sample = [
        {"uid": m.uid, "from": m.from_, "subject": m.subject, "date": str(m.date)}
        for m in msgs[:5]
    ]
    target = resolve_folder(mb, "trash")
    if apply and uids:
        if not target:
            raise RuntimeError("Could not resolve Trash folder on server (no SPECIAL-USE, no fallback match).")
        mb.move(uids, target)
    return DeleteResult(
        affected_uids=uids,
        dry_run=not apply,
        target_folder=target,
        folder=folder,
        sample=sample,
    )
```

- [ ] **Step 5: Wire `delete` into CLI**

Append to `src/mailbox_cleanup/cli.py`:

```python
from .operations.delete import run_delete
from .audit import log_action


def _require_filter(sender, subject_contains, older_than, json_mode):
    if not any([sender, subject_contains, older_than]):
        _fail(
            {"error_code": "bad_args",
             "message": "At least one filter required (--sender / --subject-contains / --older-than)"},
            4, json_mode,
        )


@cli.command("delete")
@click.option("--email", required=True)
@click.option("--folder", default="INBOX", show_default=True)
@click.option("--sender", default=None)
@click.option("--subject-contains", default=None)
@click.option("--older-than", default=None, help="e.g. 30d, 2w, 3m, 1y")
@click.option("--limit", default=None, type=int)
@click.option("--apply", is_flag=True, help="Actually move to Trash. Without --apply this is a dry-run.")
@click.option("--json", "json_mode", is_flag=True)
def delete_cmd(email, folder, sender, subject_contains, older_than, limit, apply, json_mode):
    """Soft-delete messages matching filter (move to Trash)."""
    _require_filter(sender, subject_contains, older_than, json_mode)
    try:
        creds = get_credentials(email)
    except AuthMissingError as e:
        _fail({"error_code": "auth_missing", "message": str(e)}, 3, json_mode)
        return
    try:
        with imap_connect(creds, port=_DEFAULT_PORT) as mb:
            res = run_delete(
                mb,
                folder=folder,
                sender=sender,
                subject_contains=subject_contains,
                older_than=older_than,
                apply=apply,
                limit=limit,
            )
    except Exception as e:
        _fail({"error_code": "operation_error", "message": str(e)}, 2, json_mode)
        return
    payload = {
        "ok": True,
        "schema_version": SCHEMA_VERSION,
        "subcommand": "delete",
        "dry_run": res.dry_run,
        "folder": res.folder,
        "target_folder": res.target_folder,
        "affected_count": len(res.affected_uids),
        "affected_uids": res.affected_uids,
        "sample": res.sample,
    }
    if apply:
        log_action(
            subcommand="delete",
            args={"sender": sender, "subject_contains": subject_contains, "older_than": older_than, "limit": limit},
            folder=folder,
            affected_uids=res.affected_uids,
            result="success",
        )
    if json_mode:
        click.echo(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        verb = "Moved" if apply else "Would move"
        click.echo(f"{verb} {len(res.affected_uids)} messages to {res.target_folder!r}")
```

- [ ] **Step 6: Write delete integration test**

`tests/test_delete.py`:

```python
import json
from click.testing import CliRunner
from mailbox_cleanup.cli import cli


def _make_runner_env(seeded_mailbox, monkeypatch, tmp_path):
    g = seeded_mailbox
    from mailbox_cleanup.auth import Credentials
    from mailbox_cleanup import cli as cli_mod
    monkeypatch.setattr(
        cli_mod, "get_credentials",
        lambda email: Credentials(email=g["user"], password=g["password"], server=g["host"]),
    )
    monkeypatch.setattr(cli_mod, "_DEFAULT_PORT", g["port"])
    monkeypatch.setenv("MAILBOX_CLEANUP_AUDIT_LOG", str(tmp_path / "audit.log"))


def test_delete_dry_run_does_not_modify(seeded_mailbox, monkeypatch, tmp_path):
    _make_runner_env(seeded_mailbox, monkeypatch, tmp_path)
    runner = CliRunner()
    result = runner.invoke(cli, [
        "delete", "--email", "test",
        "--sender", "newsletter@linkedin.com",
        "--json",
    ])
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["dry_run"] is True
    assert data["affected_count"] >= 1
    assert not (tmp_path / "audit.log").exists()


def test_delete_apply_moves_to_trash(seeded_mailbox, monkeypatch, tmp_path):
    _make_runner_env(seeded_mailbox, monkeypatch, tmp_path)
    runner = CliRunner()
    result = runner.invoke(cli, [
        "delete", "--email", "test",
        "--sender", "newsletter@linkedin.com",
        "--apply", "--json",
    ])
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["dry_run"] is False
    assert data["affected_count"] >= 1
    assert (tmp_path / "audit.log").exists()
    audit_line = (tmp_path / "audit.log").read_text().strip()
    audit = json.loads(audit_line)
    assert audit["subcommand"] == "delete"
    assert audit["result"] == "success"


def test_delete_without_filter_returns_4(seeded_mailbox, monkeypatch, tmp_path):
    _make_runner_env(seeded_mailbox, monkeypatch, tmp_path)
    runner = CliRunner()
    result = runner.invoke(cli, ["delete", "--email", "test", "--json"])
    assert result.exit_code == 4
```

- [ ] **Step 7: Run all tests**

```bash
uv run pytest -v
```

Expected: all green.

- [ ] **Step 8: Commit**

```bash
git add src/mailbox_cleanup/operations/ src/mailbox_cleanup/cli.py \
        tests/test_filters.py tests/test_delete.py
git commit -m "feat(delete): soft-delete with filters, dry-run by default, audit log on apply"
```

---

## Task 11: `move` subcommand

**Files:**
- Create: `src/mailbox_cleanup/operations/move.py`
- Modify: `src/mailbox_cleanup/cli.py`
- Create: `tests/test_move.py`

`move` is `delete` with an explicit target folder. Most of the work was done in Task 10 — this thin wrapper makes the operation explicit.

- [ ] **Step 1: Write failing test**

`tests/test_move.py`:

```python
import json
from click.testing import CliRunner
from mailbox_cleanup.cli import cli


def test_move_apply_moves_to_named_folder(seeded_mailbox, monkeypatch, tmp_path):
    g = seeded_mailbox
    from mailbox_cleanup.auth import Credentials
    from mailbox_cleanup import cli as cli_mod
    from imap_tools import MailBox

    # Pre-create target folder
    with MailBox(g["host"], port=g["port"]).login(g["user"], g["password"]) as mb:
        try:
            mb.folder.create("Triage")
        except Exception:
            pass

    monkeypatch.setattr(
        cli_mod, "get_credentials",
        lambda email: Credentials(email=g["user"], password=g["password"], server=g["host"]),
    )
    monkeypatch.setattr(cli_mod, "_DEFAULT_PORT", g["port"])
    monkeypatch.setenv("MAILBOX_CLEANUP_AUDIT_LOG", str(tmp_path / "audit.log"))

    runner = CliRunner()
    result = runner.invoke(cli, [
        "move", "--email", "test",
        "--sender", "alice@example.com",
        "--to", "Triage",
        "--apply", "--json",
    ])
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["dry_run"] is False
    assert data["target_folder"] == "Triage"
    assert data["affected_count"] >= 1
```

- [ ] **Step 2: Implement move operation**

`src/mailbox_cleanup/operations/move.py`:

```python
"""Move operation — same filter set as delete, but explicit target folder."""

from dataclasses import dataclass

from .filters import build_imap_search


@dataclass
class MoveResult:
    affected_uids: list[str]
    dry_run: bool
    target_folder: str
    folder: str
    sample: list[dict]


def run_move(
    mb,
    *,
    folder: str,
    target: str,
    sender: str | None = None,
    subject_contains: str | None = None,
    older_than: str | None = None,
    apply: bool = False,
    limit: int | None = None,
) -> MoveResult:
    mb.folder.set(folder)
    criteria = build_imap_search(
        sender=sender,
        subject_contains=subject_contains,
        older_than=older_than,
    )
    msgs = list(mb.fetch(criteria, headers_only=True, mark_seen=False, limit=limit, bulk=True))
    uids = [m.uid for m in msgs if m.uid]
    sample = [
        {"uid": m.uid, "from": m.from_, "subject": m.subject, "date": str(m.date)}
        for m in msgs[:5]
    ]
    if apply and uids:
        mb.move(uids, target)
    return MoveResult(
        affected_uids=uids,
        dry_run=not apply,
        target_folder=target,
        folder=folder,
        sample=sample,
    )
```

- [ ] **Step 3: Wire `move` into CLI**

Append to `src/mailbox_cleanup/cli.py`:

```python
from .operations.move import run_move


@cli.command("move")
@click.option("--email", required=True)
@click.option("--folder", default="INBOX", show_default=True)
@click.option("--to", "target", required=True, help="Destination folder.")
@click.option("--sender", default=None)
@click.option("--subject-contains", default=None)
@click.option("--older-than", default=None)
@click.option("--limit", default=None, type=int)
@click.option("--apply", is_flag=True)
@click.option("--json", "json_mode", is_flag=True)
def move_cmd(email, folder, target, sender, subject_contains, older_than, limit, apply, json_mode):
    """Move messages matching filter to target folder."""
    _require_filter(sender, subject_contains, older_than, json_mode)
    try:
        creds = get_credentials(email)
    except AuthMissingError as e:
        _fail({"error_code": "auth_missing", "message": str(e)}, 3, json_mode)
        return
    try:
        with imap_connect(creds, port=_DEFAULT_PORT) as mb:
            res = run_move(
                mb, folder=folder, target=target,
                sender=sender, subject_contains=subject_contains,
                older_than=older_than, apply=apply, limit=limit,
            )
    except Exception as e:
        _fail({"error_code": "operation_error", "message": str(e)}, 2, json_mode)
        return
    payload = {
        "ok": True,
        "schema_version": SCHEMA_VERSION,
        "subcommand": "move",
        "dry_run": res.dry_run,
        "folder": res.folder,
        "target_folder": res.target_folder,
        "affected_count": len(res.affected_uids),
        "affected_uids": res.affected_uids,
        "sample": res.sample,
    }
    if apply:
        log_action(
            subcommand="move",
            args={"to": target, "sender": sender, "subject_contains": subject_contains,
                  "older_than": older_than, "limit": limit},
            folder=folder,
            affected_uids=res.affected_uids,
            result="success",
        )
    if json_mode:
        click.echo(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        verb = "Moved" if apply else "Would move"
        click.echo(f"{verb} {len(res.affected_uids)} messages to {target!r}")
```

- [ ] **Step 4: Run tests**

```bash
uv run pytest tests/test_move.py -v
```

Expected: 1 passed.

- [ ] **Step 5: Commit**

```bash
git add src/mailbox_cleanup/operations/move.py src/mailbox_cleanup/cli.py tests/test_move.py
git commit -m "feat(move): move messages by filter to named folder"
```

---

## Task 12: `archive` subcommand

**Files:**
- Create: `src/mailbox_cleanup/operations/archive.py`
- Modify: `src/mailbox_cleanup/cli.py`
- Create: `tests/test_archive.py`

`archive` moves messages older than N into `Archive/YYYY` (per source year). Auto-creates year subfolders.

- [ ] **Step 1: Write failing test**

`tests/test_archive.py`:

```python
import json
from datetime import datetime, timezone
from unittest.mock import MagicMock
from mailbox_cleanup.operations.archive import run_archive


def _msg(uid, year):
    m = MagicMock()
    m.uid = uid
    m.from_ = "x@y.example"
    m.subject = f"msg {uid}"
    m.date = datetime(year, 6, 1, tzinfo=timezone.utc)
    return m


def test_archive_groups_by_year_and_moves(monkeypatch):
    mb = MagicMock()
    mb.fetch.return_value = [_msg("1", 2023), _msg("2", 2023), _msg("3", 2024)]
    # SPECIAL-USE detection: pretend Archive exists as plain folder
    archive_folder = MagicMock(); archive_folder.name = "Archive"; archive_folder.flags = ()
    inbox = MagicMock(); inbox.name = "INBOX"; inbox.flags = ()
    mb.folder.list.return_value = [inbox, archive_folder]
    mb.folder.exists.return_value = False  # subfolder needs creation

    res = run_archive(mb, folder="INBOX", older_than="12m",
                      apply=True, now=datetime(2026, 5, 4, tzinfo=timezone.utc))

    # Check folders created and moves issued
    created = [c.args[0] for c in mb.folder.create.call_args_list]
    assert "Archive/2023" in created
    assert "Archive/2024" in created

    move_calls = mb.move.call_args_list
    moved_targets = sorted(c.args[1] for c in move_calls)
    assert moved_targets == ["Archive/2023", "Archive/2024"]
    assert res.dry_run is False
    assert sum(len(g["uids"]) for g in res.groups) == 3


def test_archive_dry_run_does_not_create_or_move():
    mb = MagicMock()
    mb.fetch.return_value = [_msg("1", 2023)]
    archive_folder = MagicMock(); archive_folder.name = "Archive"; archive_folder.flags = ()
    mb.folder.list.return_value = [archive_folder]
    res = run_archive(mb, folder="INBOX", older_than="12m",
                      apply=False, now=datetime(2026, 5, 4, tzinfo=timezone.utc))
    mb.folder.create.assert_not_called()
    mb.move.assert_not_called()
    assert res.dry_run is True
    assert res.groups[0]["target"] == "Archive/2023"
```

- [ ] **Step 2: Implement archive operation**

`src/mailbox_cleanup/operations/archive.py`:

```python
"""Archive operation — bulk-move old messages into Archive/YYYY subfolders."""

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone

from imap_tools import AND

from ..folders import resolve_folder
from .filters import parse_age


@dataclass
class ArchiveResult:
    dry_run: bool
    folder: str
    archive_root: str | None
    groups: list[dict] = field(default_factory=list)


def run_archive(
    mb,
    *,
    folder: str,
    older_than: str,
    apply: bool = False,
    now: datetime | None = None,
) -> ArchiveResult:
    if now is None:
        now = datetime.now(timezone.utc)
    cutoff = (now - parse_age(older_than)).date()

    archive_root = resolve_folder(mb, "archive") or "Archive"
    mb.folder.set(folder)
    msgs = list(mb.fetch(AND(date_lt=cutoff), headers_only=True, mark_seen=False, bulk=True))

    by_year: dict[int, list[str]] = defaultdict(list)
    for m in msgs:
        if not m.uid or not isinstance(m.date, datetime):
            continue
        d = m.date if m.date.tzinfo else m.date.replace(tzinfo=timezone.utc)
        by_year[d.year].append(m.uid)

    groups: list[dict] = []
    for year in sorted(by_year):
        target = f"{archive_root}/{year}"
        uids = by_year[year]
        groups.append({"year": year, "target": target, "uids": uids, "count": len(uids)})
        if apply and uids:
            try:
                if not mb.folder.exists(target):
                    mb.folder.create(target)
            except Exception:
                # Some servers don't have folder.exists; create and ignore "already exists"
                try:
                    mb.folder.create(target)
                except Exception:
                    pass
            mb.move(uids, target)

    return ArchiveResult(dry_run=not apply, folder=folder, archive_root=archive_root, groups=groups)
```

- [ ] **Step 3: Wire `archive` into CLI**

Append to `src/mailbox_cleanup/cli.py`:

```python
from .operations.archive import run_archive


@cli.command("archive")
@click.option("--email", required=True)
@click.option("--folder", default="INBOX", show_default=True)
@click.option("--older-than", required=True, help="e.g. 12m, 2y")
@click.option("--apply", is_flag=True)
@click.option("--json", "json_mode", is_flag=True)
def archive_cmd(email, folder, older_than, apply, json_mode):
    """Bulk-move messages older than N into Archive/YYYY subfolders."""
    try:
        creds = get_credentials(email)
    except AuthMissingError as e:
        _fail({"error_code": "auth_missing", "message": str(e)}, 3, json_mode)
        return
    try:
        with imap_connect(creds, port=_DEFAULT_PORT) as mb:
            res = run_archive(mb, folder=folder, older_than=older_than, apply=apply)
    except Exception as e:
        _fail({"error_code": "operation_error", "message": str(e)}, 2, json_mode)
        return
    affected = sum(g["count"] for g in res.groups)
    payload = {
        "ok": True,
        "schema_version": SCHEMA_VERSION,
        "subcommand": "archive",
        "dry_run": res.dry_run,
        "folder": res.folder,
        "archive_root": res.archive_root,
        "groups": res.groups,
        "affected_count": affected,
    }
    if apply:
        log_action(
            subcommand="archive",
            args={"older_than": older_than},
            folder=folder,
            affected_uids=[uid for g in res.groups for uid in g["uids"]],
            result="success",
        )
    if json_mode:
        click.echo(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        verb = "Moved" if apply else "Would move"
        click.echo(f"{verb} {affected} messages into {res.archive_root}/<year>")
        for g in res.groups:
            click.echo(f"  {g['year']}: {g['count']} → {g['target']}")
```

- [ ] **Step 4: Run tests**

```bash
uv run pytest tests/test_archive.py -v
```

Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add src/mailbox_cleanup/operations/archive.py src/mailbox_cleanup/cli.py tests/test_archive.py
git commit -m "feat(archive): bulk-move old messages into Archive/YYYY subfolders"
```

---

## Task 13: `dedupe` subcommand

**Files:**
- Create: `src/mailbox_cleanup/operations/dedupe.py`
- Modify: `src/mailbox_cleanup/cli.py`
- Create: `tests/test_dedupe.py`

Groups by `Message-ID`, keeps the **oldest** (first delivered), moves the rest to Trash.

- [ ] **Step 1: Write failing test**

`tests/test_dedupe.py`:

```python
from datetime import datetime, timezone
from unittest.mock import MagicMock
from mailbox_cleanup.operations.dedupe import run_dedupe


def _msg(uid, mid, date):
    m = MagicMock()
    m.uid = uid
    m.headers = {"message-id": (mid,)}
    m.date = date
    m.from_ = "x@y.example"
    m.subject = "s"
    return m


def test_dedupe_keeps_oldest_drops_rest(monkeypatch):
    mb = MagicMock()
    mb.fetch.return_value = [
        _msg("1", "<dup@x>", datetime(2025, 1, 1, tzinfo=timezone.utc)),
        _msg("2", "<dup@x>", datetime(2025, 1, 2, tzinfo=timezone.utc)),
        _msg("3", "<dup@x>", datetime(2025, 1, 3, tzinfo=timezone.utc)),
        _msg("4", "<unique@x>", datetime(2025, 1, 4, tzinfo=timezone.utc)),
    ]
    trash = MagicMock(); trash.name = "Papierkorb"; trash.flags = ()
    inbox = MagicMock(); inbox.name = "INBOX"; inbox.flags = ()
    mb.folder.list.return_value = [inbox, trash]

    res = run_dedupe(mb, folder="INBOX", apply=True)
    assert sorted(res.duplicate_uids) == ["2", "3"]
    mb.move.assert_called_once()
    moved_uids, target = mb.move.call_args.args
    assert sorted(moved_uids) == ["2", "3"]
    assert target == "Papierkorb"


def test_dedupe_dry_run_does_not_move():
    mb = MagicMock()
    mb.fetch.return_value = [
        _msg("1", "<dup@x>", datetime(2025, 1, 1, tzinfo=timezone.utc)),
        _msg("2", "<dup@x>", datetime(2025, 1, 2, tzinfo=timezone.utc)),
    ]
    trash = MagicMock(); trash.name = "Trash"; trash.flags = ()
    mb.folder.list.return_value = [trash]
    res = run_dedupe(mb, folder="INBOX", apply=False)
    mb.move.assert_not_called()
    assert res.dry_run is True
    assert res.duplicate_uids == ["2"]
```

- [ ] **Step 2: Implement dedupe**

`src/mailbox_cleanup/operations/dedupe.py`:

```python
"""Dedupe operation — group by Message-ID, keep oldest, move rest to Trash."""

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone

from ..folders import resolve_folder


@dataclass
class DedupeResult:
    dry_run: bool
    folder: str
    target_folder: str | None
    groups: list[dict] = field(default_factory=list)
    duplicate_uids: list[str] = field(default_factory=list)


def run_dedupe(mb, *, folder: str = "INBOX", apply: bool = False) -> DedupeResult:
    target = resolve_folder(mb, "trash")
    mb.folder.set(folder)
    msgs = list(mb.fetch(headers_only=True, mark_seen=False, bulk=True))

    by_id: dict[str, list[tuple[datetime, str]]] = defaultdict(list)
    for m in msgs:
        if not m.uid:
            continue
        headers = m.headers or {}
        # imap-tools yields headers as {key: tuple(values)}
        mid_tuple = headers.get("message-id") or headers.get("Message-ID")
        if not mid_tuple:
            continue
        mid = mid_tuple[0] if isinstance(mid_tuple, tuple) else mid_tuple
        date = m.date if isinstance(m.date, datetime) else datetime.now(timezone.utc)
        date = date if date.tzinfo else date.replace(tzinfo=timezone.utc)
        by_id[mid].append((date, m.uid))

    groups: list[dict] = []
    drop_uids: list[str] = []
    for mid, entries in by_id.items():
        if len(entries) < 2:
            continue
        entries.sort()  # oldest first
        keep = entries[0][1]
        drops = [uid for _, uid in entries[1:]]
        groups.append({"message_id": mid, "keep": keep, "drop": drops})
        drop_uids.extend(drops)

    if apply and drop_uids:
        if not target:
            raise RuntimeError("Could not resolve Trash folder.")
        mb.move(drop_uids, target)

    return DedupeResult(
        dry_run=not apply,
        folder=folder,
        target_folder=target,
        groups=groups,
        duplicate_uids=drop_uids,
    )
```

- [ ] **Step 3: Wire `dedupe` into CLI**

Append to `src/mailbox_cleanup/cli.py`:

```python
from .operations.dedupe import run_dedupe


@cli.command("dedupe")
@click.option("--email", required=True)
@click.option("--folder", default="INBOX", show_default=True)
@click.option("--apply", is_flag=True)
@click.option("--json", "json_mode", is_flag=True)
def dedupe_cmd(email, folder, apply, json_mode):
    """Move duplicate-by-Message-ID copies to Trash, keep the oldest."""
    try:
        creds = get_credentials(email)
    except AuthMissingError as e:
        _fail({"error_code": "auth_missing", "message": str(e)}, 3, json_mode)
        return
    try:
        with imap_connect(creds, port=_DEFAULT_PORT) as mb:
            res = run_dedupe(mb, folder=folder, apply=apply)
    except Exception as e:
        _fail({"error_code": "operation_error", "message": str(e)}, 2, json_mode)
        return
    payload = {
        "ok": True,
        "schema_version": SCHEMA_VERSION,
        "subcommand": "dedupe",
        "dry_run": res.dry_run,
        "folder": res.folder,
        "target_folder": res.target_folder,
        "duplicate_count": len(res.duplicate_uids),
        "groups": res.groups,
    }
    if apply:
        log_action(
            subcommand="dedupe", args={}, folder=folder,
            affected_uids=res.duplicate_uids, result="success",
        )
    if json_mode:
        click.echo(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        verb = "Moved" if apply else "Would move"
        click.echo(f"{verb} {len(res.duplicate_uids)} duplicates from {len(res.groups)} groups to Trash")
```

- [ ] **Step 4: Run tests**

```bash
uv run pytest tests/test_dedupe.py -v
```

Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add src/mailbox_cleanup/operations/dedupe.py src/mailbox_cleanup/cli.py tests/test_dedupe.py
git commit -m "feat(dedupe): drop Message-ID duplicates, keep oldest"
```

---

## Task 14: `attachments` subcommand

**Files:**
- Create: `src/mailbox_cleanup/operations/attachments.py`
- Modify: `src/mailbox_cleanup/cli.py`
- Create: `tests/test_attachments.py`

`attachments` finds messages with size > N. With `--strip`, removes the body (RFC 5322 message bodies cannot be edited in-place via IMAP — instead, append a stripped copy and move original to Trash). For v1, **only the find/list operation** is implemented; stripping is deferred.

- [ ] **Step 1: Write failing test**

`tests/test_attachments.py`:

```python
import json
from datetime import datetime, timezone
from unittest.mock import MagicMock
from click.testing import CliRunner

from mailbox_cleanup.cli import cli
from mailbox_cleanup.operations.attachments import find_large_messages


def _msg(uid, size, date):
    m = MagicMock()
    m.uid = uid
    m.from_ = "x@y.example"
    m.subject = "huge"
    m.size = size
    m.date = date
    return m


def test_find_large_messages_size_only():
    msgs = [
        _msg("1", 5_000_000, datetime(2025, 1, 1, tzinfo=timezone.utc)),
        _msg("2", 15_000_000, datetime(2025, 1, 1, tzinfo=timezone.utc)),
        _msg("3", 25_000_000, datetime(2025, 1, 1, tzinfo=timezone.utc)),
    ]
    res = find_large_messages(msgs, size_gt_bytes=10 * 1024 * 1024)
    assert sorted(m.uid for m in res) == ["2", "3"]


def test_find_large_messages_with_age_filter():
    now = datetime(2026, 5, 4, tzinfo=timezone.utc)
    msgs = [
        _msg("1", 15_000_000, datetime(2025, 1, 1, tzinfo=timezone.utc)),
        _msg("2", 15_000_000, datetime(2026, 4, 1, tzinfo=timezone.utc)),
    ]
    res = find_large_messages(msgs, size_gt_bytes=10 * 1024 * 1024, older_than="6m", now=now)
    assert [m.uid for m in res] == ["1"]


def test_attachments_cli_lists_only_no_strip(seeded_mailbox, monkeypatch, tmp_path):
    g = seeded_mailbox
    from mailbox_cleanup.auth import Credentials
    from mailbox_cleanup import cli as cli_mod
    monkeypatch.setattr(
        cli_mod, "get_credentials",
        lambda email: Credentials(email=g["user"], password=g["password"], server=g["host"]),
    )
    monkeypatch.setattr(cli_mod, "_DEFAULT_PORT", g["port"])
    monkeypatch.setenv("MAILBOX_CLEANUP_AUDIT_LOG", str(tmp_path / "audit.log"))

    runner = CliRunner()
    result = runner.invoke(cli, [
        "attachments", "--email", "test",
        "--size-gt=1b",  # all fixture mails will exceed
        "--json",
    ])
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert "candidates" in data
    assert data["dry_run"] is True
```

- [ ] **Step 2: Implement attachments operation**

`src/mailbox_cleanup/operations/attachments.py`:

```python
"""Attachments operation — find large messages (strip deferred to v2)."""

import re
from dataclasses import dataclass
from datetime import datetime, timezone

from .filters import parse_age


_SIZE_RE = re.compile(r"^(\d+)\s*(b|kb|mb|gb)?$", re.IGNORECASE)
_SIZE_MULT = {"b": 1, "kb": 1024, "mb": 1024**2, "gb": 1024**3, None: 1}


def parse_size(spec: str) -> int:
    m = _SIZE_RE.match(spec.strip())
    if not m:
        raise ValueError(f"Bad --size-gt spec: {spec!r}; expected e.g. 10mb, 500kb")
    n = int(m.group(1))
    unit = (m.group(2) or "b").lower()
    return n * _SIZE_MULT[unit]


@dataclass
class AttachmentsResult:
    dry_run: bool
    folder: str
    candidates: list[dict]


def find_large_messages(
    messages,
    *,
    size_gt_bytes: int,
    older_than: str | None = None,
    now: datetime | None = None,
):
    if now is None:
        now = datetime.now(timezone.utc)
    cutoff = None
    if older_than:
        cutoff = now - parse_age(older_than)

    out = []
    for m in messages:
        size = getattr(m, "size", 0) or 0
        if size <= size_gt_bytes:
            continue
        if cutoff is not None and isinstance(m.date, datetime):
            d = m.date if m.date.tzinfo else m.date.replace(tzinfo=timezone.utc)
            if d > cutoff:
                continue
        out.append(m)
    return out


def run_attachments(mb, *, folder: str, size_gt: str, older_than: str | None) -> AttachmentsResult:
    mb.folder.set(folder)
    size_gt_bytes = parse_size(size_gt)
    msgs = list(mb.fetch(headers_only=True, mark_seen=False, bulk=True))
    matches = find_large_messages(msgs, size_gt_bytes=size_gt_bytes, older_than=older_than)
    candidates = [
        {
            "uid": m.uid,
            "from": m.from_,
            "subject": m.subject,
            "size_mb": round((m.size or 0) / 1024 / 1024, 1),
            "date": str(m.date),
        }
        for m in matches
    ]
    return AttachmentsResult(dry_run=True, folder=folder, candidates=candidates)
```

- [ ] **Step 3: Wire `attachments` into CLI**

Append to `src/mailbox_cleanup/cli.py`:

```python
from .operations.attachments import run_attachments


@cli.command("attachments")
@click.option("--email", required=True)
@click.option("--folder", default="INBOX", show_default=True)
@click.option("--size-gt", default="10mb", show_default=True)
@click.option("--older-than", default=None)
@click.option("--json", "json_mode", is_flag=True)
def attachments_cmd(email, folder, size_gt, older_than, json_mode):
    """Find large messages. Stripping deferred to v2 — this lists candidates only."""
    try:
        creds = get_credentials(email)
    except AuthMissingError as e:
        _fail({"error_code": "auth_missing", "message": str(e)}, 3, json_mode)
        return
    try:
        with imap_connect(creds, port=_DEFAULT_PORT) as mb:
            res = run_attachments(mb, folder=folder, size_gt=size_gt, older_than=older_than)
    except Exception as e:
        _fail({"error_code": "operation_error", "message": str(e)}, 2, json_mode)
        return
    payload = {
        "ok": True,
        "schema_version": SCHEMA_VERSION,
        "subcommand": "attachments",
        "dry_run": res.dry_run,
        "folder": res.folder,
        "candidate_count": len(res.candidates),
        "candidates": res.candidates,
        "note": "v1 lists candidates only; pipe to `delete --sender=... --apply` to remove specific senders.",
    }
    if json_mode:
        click.echo(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        click.echo(f"Found {len(res.candidates)} large messages:")
        for c in res.candidates[:20]:
            click.echo(f"  {c['size_mb']:>6.1f} MB  {c['from']}  {c['subject']}")
```

- [ ] **Step 4: Run tests**

```bash
uv run pytest tests/test_attachments.py -v
```

Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add src/mailbox_cleanup/operations/attachments.py src/mailbox_cleanup/cli.py tests/test_attachments.py
git commit -m "feat(attachments): list large-message candidates (strip deferred to v2)"
```

---

## Task 15: `unsubscribe` subcommand

**Files:**
- Create: `src/mailbox_cleanup/operations/unsubscribe.py`
- Modify: `src/mailbox_cleanup/cli.py`
- Create: `tests/test_unsubscribe.py`

Parses `List-Unsubscribe` per RFC 2369, prefers RFC 8058 one-click HTTPS POST. Supports `https:` and `mailto:`. SMTP for mailto reuses IMAP credentials (IONOS uses same password).

- [ ] **Step 1: Write failing tests**

`tests/test_unsubscribe.py`:

```python
from unittest.mock import MagicMock, patch
from mailbox_cleanup.operations.unsubscribe import (
    parse_list_unsubscribe,
    UnsubAction,
    perform_unsubscribe,
)


def test_parse_https_only():
    actions = parse_list_unsubscribe(
        list_unsubscribe="<https://example.com/unsub?t=abc>",
        list_unsubscribe_post=None,
    )
    assert any(a.kind == "https" and a.target == "https://example.com/unsub?t=abc" for a in actions)


def test_parse_mailto_only():
    actions = parse_list_unsubscribe(
        list_unsubscribe="<mailto:unsub@x.example?subject=unsubscribe>",
        list_unsubscribe_post=None,
    )
    assert any(a.kind == "mailto" and a.target == "unsub@x.example" for a in actions)


def test_parse_both_https_preferred():
    actions = parse_list_unsubscribe(
        list_unsubscribe="<mailto:u@x>, <https://x.com/unsub>",
        list_unsubscribe_post="List-Unsubscribe=One-Click",
    )
    https = [a for a in actions if a.kind == "https"][0]
    assert https.one_click is True


def test_perform_https_one_click_uses_post():
    action = UnsubAction(kind="https", target="https://x.com/unsub", one_click=True)
    with patch("mailbox_cleanup.operations.unsubscribe.requests") as r:
        r.post.return_value = MagicMock(status_code=200)
        ok, info = perform_unsubscribe(action, smtp_sender=None)
    assert ok is True
    r.post.assert_called_once()
    assert "List-Unsubscribe=One-Click" in r.post.call_args.kwargs["data"]


def test_perform_https_get_when_no_one_click():
    action = UnsubAction(kind="https", target="https://x.com/unsub", one_click=False)
    with patch("mailbox_cleanup.operations.unsubscribe.requests") as r:
        r.get.return_value = MagicMock(status_code=200)
        ok, info = perform_unsubscribe(action, smtp_sender=None)
    assert ok is True
    r.get.assert_called_once()
```

- [ ] **Step 2: Implement unsubscribe**

`src/mailbox_cleanup/operations/unsubscribe.py`:

```python
"""Parse and execute List-Unsubscribe per RFC 2369 / RFC 8058."""

import re
import smtplib
from dataclasses import dataclass
from email.message import EmailMessage

import requests

from ..folders import resolve_folder
from .filters import build_imap_search

_LINK_RE = re.compile(r"<([^>]+)>")


@dataclass
class UnsubAction:
    kind: str         # "https" or "mailto"
    target: str       # URL or mail address
    one_click: bool   # only relevant for https


def parse_list_unsubscribe(
    *,
    list_unsubscribe: str,
    list_unsubscribe_post: str | None,
) -> list[UnsubAction]:
    actions: list[UnsubAction] = []
    one_click = bool(
        list_unsubscribe_post and "List-Unsubscribe=One-Click" in list_unsubscribe_post
    )
    for raw in _LINK_RE.findall(list_unsubscribe or ""):
        raw = raw.strip()
        if raw.startswith("mailto:"):
            target = raw[len("mailto:"):].split("?", 1)[0]
            actions.append(UnsubAction(kind="mailto", target=target, one_click=False))
        elif raw.startswith(("http://", "https://")):
            actions.append(UnsubAction(kind="https", target=raw, one_click=one_click))
    # Prefer https first, then mailto
    actions.sort(key=lambda a: 0 if a.kind == "https" else 1)
    return actions


def perform_unsubscribe(
    action: UnsubAction,
    *,
    smtp_sender: str | None,
    smtp_password: str | None = None,
    smtp_host: str = "smtp.ionos.de",
    smtp_port: int = 587,
    timeout: float = 15.0,
) -> tuple[bool, str]:
    if action.kind == "https":
        try:
            if action.one_click:
                resp = requests.post(
                    action.target,
                    data="List-Unsubscribe=One-Click",
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                    timeout=timeout,
                )
            else:
                resp = requests.get(action.target, timeout=timeout)
            return resp.status_code < 400, f"HTTP {resp.status_code}"
        except Exception as e:
            return False, f"HTTPS error: {e}"
    elif action.kind == "mailto":
        if not smtp_sender or not smtp_password:
            return False, "SMTP credentials missing for mailto unsubscribe"
        msg = EmailMessage()
        msg["From"] = smtp_sender
        msg["To"] = action.target
        msg["Subject"] = "unsubscribe"
        msg.set_content("unsubscribe")
        try:
            with smtplib.SMTP(smtp_host, smtp_port, timeout=timeout) as s:
                s.starttls()
                s.login(smtp_sender, smtp_password)
                s.send_message(msg)
            return True, "SMTP sent"
        except Exception as e:
            return False, f"SMTP error: {e}"
    return False, f"Unknown action kind: {action.kind}"


def collect_unsub_targets(mb, *, sender: str, folder: str = "INBOX") -> list[dict]:
    """Find messages from sender, parse their List-Unsubscribe headers, return targets."""
    mb.folder.set(folder)
    msgs = list(mb.fetch(
        build_imap_search(sender=sender),
        headers_only=True, mark_seen=False, bulk=True,
    ))
    seen: set[tuple[str, str]] = set()
    out: list[dict] = []
    uids: list[str] = []
    for m in msgs:
        if m.uid:
            uids.append(m.uid)
        headers = m.headers or {}
        lu = headers.get("list-unsubscribe") or headers.get("List-Unsubscribe")
        lup = headers.get("list-unsubscribe-post") or headers.get("List-Unsubscribe-Post")
        if not lu:
            continue
        lu_val = lu[0] if isinstance(lu, tuple) else lu
        lup_val = (lup[0] if isinstance(lup, tuple) else lup) if lup else None
        for action in parse_list_unsubscribe(list_unsubscribe=lu_val, list_unsubscribe_post=lup_val):
            key = (action.kind, action.target)
            if key in seen:
                continue
            seen.add(key)
            out.append({"kind": action.kind, "target": action.target, "one_click": action.one_click})
    return {"uids": uids, "actions": out}
```

- [ ] **Step 3: Wire `unsubscribe` into CLI**

Append to `src/mailbox_cleanup/cli.py`:

```python
from .operations.unsubscribe import collect_unsub_targets, perform_unsubscribe, UnsubAction


@cli.command("unsubscribe")
@click.option("--email", required=True)
@click.option("--folder", default="INBOX", show_default=True)
@click.option("--sender", required=True)
@click.option("--apply", is_flag=True)
@click.option("--json", "json_mode", is_flag=True)
def unsubscribe_cmd(email, folder, sender, apply, json_mode):
    """Parse List-Unsubscribe header for sender, optionally execute (HTTPS or mailto)."""
    try:
        creds = get_credentials(email)
    except AuthMissingError as e:
        _fail({"error_code": "auth_missing", "message": str(e)}, 3, json_mode)
        return
    try:
        with imap_connect(creds, port=_DEFAULT_PORT) as mb:
            data = collect_unsub_targets(mb, sender=sender, folder=folder)
            uids = data["uids"]
            actions = data["actions"]
            results: list[dict] = []
            if apply:
                # Take the first (preferred) action — already sorted https-first
                if actions:
                    a = UnsubAction(**{k: actions[0][k] for k in ("kind", "target", "one_click")})
                    ok, info = perform_unsubscribe(
                        a,
                        smtp_sender=creds.email,
                        smtp_password=creds.password,
                    )
                    results.append({"action": actions[0], "ok": ok, "info": info})
                # Move matching messages to Trash regardless of unsubscribe success
                trash = resolve_folder(mb, "trash")
                if trash and uids:
                    mb.move(uids, trash)
    except Exception as e:
        _fail({"error_code": "operation_error", "message": str(e)}, 2, json_mode)
        return
    payload = {
        "ok": True,
        "schema_version": SCHEMA_VERSION,
        "subcommand": "unsubscribe",
        "dry_run": not apply,
        "folder": folder,
        "sender": sender,
        "matching_count": len(uids),
        "actions": actions,
        "results": results,
    }
    if apply:
        log_action(
            subcommand="unsubscribe",
            args={"sender": sender},
            folder=folder,
            affected_uids=uids,
            result="success" if not results or results[0]["ok"] else "partial",
        )
    if json_mode:
        click.echo(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        verb = "Performed" if apply else "Would attempt"
        click.echo(f"{verb} unsubscribe for {sender}: {len(actions)} action(s) found, {len(uids)} matching messages")


# Make resolve_folder importable here for the command above
from .folders import resolve_folder
```

- [ ] **Step 4: Run tests**

```bash
uv run pytest tests/test_unsubscribe.py -v
```

Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add src/mailbox_cleanup/operations/unsubscribe.py src/mailbox_cleanup/cli.py tests/test_unsubscribe.py
git commit -m "feat(unsubscribe): RFC 2369 / RFC 8058 one-click HTTPS, mailto fallback via SMTP"
```

---

## Task 16: `bounces` subcommand

**Files:**
- Create: `src/mailbox_cleanup/operations/bounces.py`
- Modify: `src/mailbox_cleanup/cli.py`
- Create: `tests/test_bounces.py`

Find bounce/auto-reply messages using the rules already in `classify.py::is_bounce`. Move to Trash on `--apply`.

- [ ] **Step 1: Write failing test**

`tests/test_bounces.py`:

```python
import json
from click.testing import CliRunner
from mailbox_cleanup.cli import cli


def _env(seeded_mailbox, monkeypatch, tmp_path):
    g = seeded_mailbox
    from mailbox_cleanup.auth import Credentials
    from mailbox_cleanup import cli as cli_mod
    monkeypatch.setattr(
        cli_mod, "get_credentials",
        lambda email: Credentials(email=g["user"], password=g["password"], server=g["host"]),
    )
    monkeypatch.setattr(cli_mod, "_DEFAULT_PORT", g["port"])
    monkeypatch.setenv("MAILBOX_CLEANUP_AUDIT_LOG", str(tmp_path / "audit.log"))


def test_bounces_dry_run_finds_mailer_daemon(seeded_mailbox, monkeypatch, tmp_path):
    _env(seeded_mailbox, monkeypatch, tmp_path)
    runner = CliRunner()
    result = runner.invoke(cli, ["bounces", "--email", "test", "--json"])
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["dry_run"] is True
    assert data["affected_count"] >= 1


def test_bounces_apply_moves_to_trash(seeded_mailbox, monkeypatch, tmp_path):
    _env(seeded_mailbox, monkeypatch, tmp_path)
    runner = CliRunner()
    result = runner.invoke(cli, ["bounces", "--email", "test", "--apply", "--json"])
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["dry_run"] is False
    assert data["affected_count"] >= 1
    assert (tmp_path / "audit.log").exists()
```

- [ ] **Step 2: Implement bounces operation**

`src/mailbox_cleanup/operations/bounces.py`:

```python
"""Bounces operation — find bounce / auto-reply messages, optionally move to Trash."""

from dataclasses import dataclass

from ..classify import is_bounce
from ..folders import resolve_folder


@dataclass
class BouncesResult:
    dry_run: bool
    folder: str
    target_folder: str | None
    affected_uids: list[str]
    sample: list[dict]


def run_bounces(mb, *, folder: str = "INBOX", apply: bool = False) -> BouncesResult:
    target = resolve_folder(mb, "trash")
    mb.folder.set(folder)
    msgs = list(mb.fetch(headers_only=True, mark_seen=False, bulk=True))
    matched = [
        m for m in msgs
        if is_bounce(from_addr=(m.from_ or ""), subject=(m.subject or ""), headers={})
    ]
    uids = [m.uid for m in matched if m.uid]
    sample = [{"uid": m.uid, "from": m.from_, "subject": m.subject} for m in matched[:5]]
    if apply and uids:
        if not target:
            raise RuntimeError("Could not resolve Trash folder.")
        mb.move(uids, target)
    return BouncesResult(
        dry_run=not apply, folder=folder, target_folder=target,
        affected_uids=uids, sample=sample,
    )
```

- [ ] **Step 3: Wire `bounces` into CLI**

Append to `src/mailbox_cleanup/cli.py`:

```python
from .operations.bounces import run_bounces


@cli.command("bounces")
@click.option("--email", required=True)
@click.option("--folder", default="INBOX", show_default=True)
@click.option("--apply", is_flag=True)
@click.option("--json", "json_mode", is_flag=True)
def bounces_cmd(email, folder, apply, json_mode):
    """Find bounce/auto-reply messages, optionally move to Trash."""
    try:
        creds = get_credentials(email)
    except AuthMissingError as e:
        _fail({"error_code": "auth_missing", "message": str(e)}, 3, json_mode)
        return
    try:
        with imap_connect(creds, port=_DEFAULT_PORT) as mb:
            res = run_bounces(mb, folder=folder, apply=apply)
    except Exception as e:
        _fail({"error_code": "operation_error", "message": str(e)}, 2, json_mode)
        return
    payload = {
        "ok": True,
        "schema_version": SCHEMA_VERSION,
        "subcommand": "bounces",
        "dry_run": res.dry_run,
        "folder": res.folder,
        "target_folder": res.target_folder,
        "affected_count": len(res.affected_uids),
        "affected_uids": res.affected_uids,
        "sample": res.sample,
    }
    if apply:
        log_action(
            subcommand="bounces", args={}, folder=folder,
            affected_uids=res.affected_uids, result="success",
        )
    if json_mode:
        click.echo(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        verb = "Moved" if apply else "Would move"
        click.echo(f"{verb} {len(res.affected_uids)} bounce/auto-reply messages to Trash")
```

- [ ] **Step 4: Run all tests**

```bash
uv run pytest -v
```

Expected: all green.

- [ ] **Step 5: Commit**

```bash
git add src/mailbox_cleanup/operations/bounces.py src/mailbox_cleanup/cli.py tests/test_bounces.py
git commit -m "feat(bounces): find and soft-delete bounce/auto-reply messages"
```

---

## Task 17: GitHub Actions CI

**Files:**
- Create: `.github/workflows/ci.yml`

CI runs `ruff check`, `ruff format --check`, and `pytest` (unit + integration with Greenmail).

- [ ] **Step 1: Create workflow**

`.github/workflows/ci.yml`:

```yaml
name: CI

on:
  push:
    branches: [main]
  pull_request:

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - name: Install uv
        uses: astral-sh/setup-uv@v3
        with:
          enable-cache: true

      - name: Set up Python 3.11
        run: uv python install 3.11

      - name: Install dependencies
        run: uv sync --extra dev

      - name: Lint
        run: uv run ruff check .

      - name: Format check
        run: uv run ruff format --check .

      - name: Run tests (Docker IMAP starts as part of fixture)
        run: uv run pytest -v
```

- [ ] **Step 2: Run ruff format locally and fix any issues**

```bash
uv run ruff format .
uv run ruff check . --fix
uv run pytest -v
```

Expected: format applied, lint clean, all tests pass.

- [ ] **Step 3: Commit**

```bash
git add .github/workflows/ci.yml
# include any ruff formatting changes
git add -u
git commit -m "ci: GitHub Actions running ruff + pytest with Greenmail integration"
```

- [ ] **Step 4: Push and verify CI green**

```bash
gh repo create mailbox-cleanup --private --source=. --remote=origin --push
gh run watch
```

Expected: CI run completes green. If `gh repo create` is not needed (repo already exists), use `git push -u origin main`.

---

## Task 18: Smoke test against IONOS (manual checklist)

**Files:**
- Create: `docs/smoke-test.md`

Documented procedure for verifying the CLI against the real IONOS account. Read-only operations only.

- [ ] **Step 1: Create checklist**

`docs/smoke-test.md`:

````markdown
# IONOS Smoke Test

**Goal:** Verify CLI works end-to-end against the real IONOS mailbox using read-only operations only.

**Pre-conditions:**
- `mailbox-cleanup` installed (`uv tool install --editable .`)
- Network access to `imap.ionos.de:993`

## Steps

1. **Set credentials**

   ```bash
   mailbox-cleanup auth set --email you@example.com --server imap.ionos.de
   ```
   At the password prompt, enter the IONOS mailbox password.

2. **Test connection**

   ```bash
   mailbox-cleanup auth test --email you@example.com --json | jq '.ok, .folders[]'
   ```
   Expected: `true` followed by folder names (INBOX, Sent, Papierkorb, ...).

3. **Scan INBOX (read-only)**

   ```bash
   mailbox-cleanup scan --email you@example.com --json | jq '.total_messages, .size_total_mb'
   ```
   Expected: integer count and MB total.

4. **Top senders**

   ```bash
   mailbox-cleanup senders --email you@example.com --top 10 --json | jq '.senders'
   ```
   Expected: list of 10 sender objects.

5. **Find large attachments (read-only)**

   ```bash
   mailbox-cleanup attachments --email you@example.com --size-gt 10mb --json | jq '.candidate_count'
   ```

6. **Dry-run delete (no --apply!)**

   ```bash
   mailbox-cleanup delete --email you@example.com --sender notifications@github.com --json | jq '.dry_run, .affected_count'
   ```
   Expected: `true` and a count. Mailbox is unchanged.

## Pass criteria

- All 6 steps complete without errors
- No `--apply` flag used anywhere — mailbox state unchanged
- Audit log empty (no operations performed)
````

- [ ] **Step 2: Commit**

```bash
git add docs/smoke-test.md
git commit -m "docs: IONOS smoke-test checklist (read-only steps only)"
```

---

## Task 19: Claude Code Skill

**Files:**
- Create: `~/.claude/skills/mailbox-cleanup/SKILL.md`
- Create: `~/.claude/skills/mailbox-cleanup/README.md` (linked from SKILL.md for deeper reference)

The Skill is the conversational orchestrator described in spec §8. It calls the CLI, parses JSON, presents Markdown, asks for confirmation before any `--apply`.

- [ ] **Step 1: Create Skill directory**

```bash
mkdir -p ~/.claude/skills/mailbox-cleanup
```

- [ ] **Step 2: Write `SKILL.md`**

`~/.claude/skills/mailbox-cleanup/SKILL.md`:

````markdown
---
name: mailbox-cleanup
description: Discover and clean up the IONOS IMAP mailbox you@example.com via the `mailbox-cleanup` CLI. Use when the user wants to triage, scan, delete, archive, or unsubscribe from messages in their mail account. Always shows dry-run preview before any destructive operation.
---

# mailbox-cleanup

Conversational orchestrator over the `mailbox-cleanup` CLI. Wraps discovery → preview → apply loops with safety checks.

## Account

Single IONOS mailbox: `you@example.com`. The CLI reads credentials from macOS Keychain.

## Required CLI version

Schema version 1. The CLI emits `"schema_version": 1` in every JSON response — if it doesn't match, abort and tell the user to update the CLI.

## Setup check (run first, every session)

Before any operation, verify the CLI is reachable and authenticated:

```bash
mailbox-cleanup auth test --email you@example.com --json
```

- Exit 0 with `"ok": true`: continue.
- Exit 3 (`auth_missing`): tell the user to run `mailbox-cleanup auth set --email you@example.com --server imap.ionos.de` in their terminal. Do not proceed.
- Exit 2 (connection): show the message; do not retry blindly.

## Standard flow

1. Run `mailbox-cleanup scan --email you@example.com --json`.
2. Validate `schema_version == 1`. Otherwise abort.
3. Render a German Markdown summary:

   ```
   Mailbox: <total_messages> Nachrichten, <size_total_mb> MB

   Kategorien:
     1. Newsletter: <count> (Top-Sender: ...)
     2. Automatisierte Notifications: <count>
     3. Bounces / Auto-Replies: <count>
     4. Große Anhänge (>10 MB): <count> Nachrichten, <size_mb> MB
     5. Alte Nachrichten: <older_than_12m> älter als 12 Monate
     6. Duplikate: <count>

   Empfehlungen:
     [1] <recommendations[0]>
     [2] <recommendations[1]>
     ...
   ```

4. Ask: **"Welche Kategorie / Empfehlung willst du angehen?"**
5. When the user picks an operation:
   - Always run the CLI **without `--apply`** first (dry-run)
   - Render the preview: count + first 5 sample messages
   - Ask: **"Apply?"**
   - Only on explicit confirmation, run again with `--apply`
6. After `--apply`, show the result count and tell the user the audit log is at `~/.mailbox-cleanup/audit.log`.
7. Loop back to step 4 for the next category.

## Subcommand cheat sheet

| User intent | Command |
|-------------|---------|
| "Scan" / "Was ist drin?" | `mailbox-cleanup scan --email you@example.com --json` |
| "Wer schickt am meisten?" | `mailbox-cleanup senders --email you@example.com --top 20 --json` |
| "Lösch alles von X" | `mailbox-cleanup delete --email you@example.com --sender X --json` (then `--apply`) |
| "Alles älter als 1 Jahr archivieren" | `mailbox-cleanup archive --email you@example.com --older-than 12m --json` |
| "Vom Newsletter X abmelden" | `mailbox-cleanup unsubscribe --email you@example.com --sender X --json` |
| "Bounces wegräumen" | `mailbox-cleanup bounces --email you@example.com --json` |
| "Duplikate finden" | `mailbox-cleanup dedupe --email you@example.com --json` |
| "Große Anhänge zeigen" | `mailbox-cleanup attachments --email you@example.com --size-gt 10mb --json` |

## Exit codes

| Code | Meaning | What to do |
|------|---------|------------|
| 0 | Success | Continue |
| 2 | Connection error | Show stderr, do not retry blindly |
| 3 | Auth missing | Tell user to run `auth set` |
| 4 | Bad arguments | Show stderr; you used the CLI wrong, fix the call |
| 5 | Partial failure | Show audit log path, summarize successes/failures |

## Hard rules

1. **Never call any subcommand with `--apply` without showing a dry-run preview first and getting explicit "ja" / "yes" / "apply" from the user.**
2. **Never invent UID lists or counts.** Always use the JSON returned by the CLI.
3. **Never edit the audit log.** It is append-only forensics.
4. **All destructive operations move to Trash.** v1 has no hard-delete; if the user asks "wirklich löschen", explain that v1 only soft-deletes and Trash is purged by IONOS retention.
````

- [ ] **Step 3: Manual verification in Claude Code**

In a fresh Claude Code session in any directory, type the user prompt:

```
Bitte räum meine Mailbox auf
```

Verify Claude:
1. Invokes the `mailbox-cleanup` Skill.
2. Runs `auth test`, then `scan`.
3. Renders the German summary.
4. Asks for the user's choice.
5. For any chosen operation, runs dry-run first, shows preview, asks for confirmation.

If any step is wrong, edit `SKILL.md` and re-test.

- [ ] **Step 4: Commit Skill into the project for reference**

The Skill lives in `~/.claude/skills/`, but a copy in the project repo makes it versionable.

```bash
mkdir -p skill
cp ~/.claude/skills/mailbox-cleanup/SKILL.md skill/SKILL.md
git add skill/SKILL.md
git commit -m "feat(skill): Claude Code skill for orchestrating mailbox-cleanup CLI"
```

---

## Done criteria

After Task 19 the project is v1 complete:

- All 11 subcommands from spec §6 implemented (`auth set`, `auth test`, `scan`, `senders`, `delete`, `move`, `archive`, `unsubscribe`, `dedupe`, `attachments`, `bounces`)
- Tests cover every operation (unit + Greenmail integration)
- CI green on push to `main`
- Smoke test executed once against IONOS read-only
- Skill installed and verified in a Claude Code session
- Audit log functioning at `~/.mailbox-cleanup/audit.log`

**v2 deferred (per spec §11):** attachment stripping, fuzzy duplicate detection, Marvin cron, multi-account.
