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


@pytest.fixture(autouse=True)
def _isolated_audit_log(tmp_path, monkeypatch):
    """No test reaches the real ~/.mailbox-cleanup. Since #75 every CLI start tightens
    the audit log's modes, so a test that invoked the CLI without an override changed
    the developer's real directory (observed on the M2, 2026-10-20). Subprocess tests
    inherit the env var. A test that needs the default location overrides both."""
    from mailbox_cleanup import audit

    monkeypatch.setenv(audit.AUDIT_LOG_PATH_ENV, str(tmp_path / "audit-isolated.log"))
    monkeypatch.setattr(audit, "DEFAULT_AUDIT_LOG", tmp_path / "home" / "audit.log")


@pytest.fixture(autouse=True)
def _no_installed_bot_profile(tmp_path, monkeypatch):
    """No test reads the machine's real profile anchor or bot group (#76): on a Mac where
    the bot is installed, the suite would otherwise run under that profile. A refusal
    record would go to the caller's real home, so that is redirected too. Tests that need
    an anchor or a group set them up themselves (tests/test_bot_profile.py)."""
    from mailbox_cleanup import profile

    monkeypatch.setattr(profile, "ANCHOR_DIR", tmp_path / "no-anchor" / "mailbox-autopilot")
    monkeypatch.setattr(profile, "BOT_GROUP", "mailbox-autopilot-bot-absent-in-tests")
    monkeypatch.setattr(profile, "_home_of", lambda uid: tmp_path / "pwd-home")


def _imap_ready(host: str, port: int, timeout: float = 2.0) -> bool:
    """Greenmail opens the TCP port before the IMAP listener is fully up.
    Verify by reading the IMAP greeting line."""
    try:
        with socket.create_connection((host, port), timeout=timeout) as s:
            s.settimeout(timeout)
            data = s.recv(64)
            return data.startswith(b"* OK")
    except OSError:
        return False


@pytest.fixture(scope="session")
def greenmail():
    """Start a Greenmail IMAP/SMTP server in Docker for the test session."""
    if os.environ.get("SKIP_DOCKER_TESTS"):
        pytest.skip("SKIP_DOCKER_TESTS set")

    already_up = _imap_ready("127.0.0.1", IMAP_PORT)
    if not already_up:
        subprocess.run(
            ["docker", "compose", "-f", str(COMPOSE_FILE), "up", "-d"],
            check=True,
        )
        # Wait for IMAP listener to actually accept and greet
        for _ in range(60):
            if _imap_ready("127.0.0.1", IMAP_PORT):
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

    # A fixed local hostname: without it the client calls socket.getfqdn(), which can take
    # 30 s on a network with slow reverse DNS, and GreenMail drops the idle connection.
    smtp = smtplib.SMTP("127.0.0.1", 3025, local_hostname="localhost")
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
def _outbox_is_the_test_tmp_dir(tmp_path, monkeypatch):
    """`manage draft` and `manage compose` read their text from the tool's outbox folder
    only. In tests that folder is the test's own tmp_path, so a body file a test writes
    there is readable and nothing touches the real ~/.mailbox-cleanup/outbox."""
    from mailbox_cleanup.manage import bodyfile

    monkeypatch.setattr(bodyfile, "outbox_dir", lambda: str(tmp_path))


@pytest.fixture(autouse=True)
def _disable_ssl_for_tests(monkeypatch):
    """All tests run against plain IMAP — set env so imap_connect uses MailBoxUnencrypted."""
    monkeypatch.setenv("MAILBOX_CLEANUP_SSL", "0")


@pytest.fixture(autouse=True)
def _isolate_sources_file(tmp_path, monkeypatch):
    """No test may read the owner's real ~/.mailbox-cleanup/sources.json. Tests that need
    a sources file set MAILBOX_CLEANUP_SOURCES themselves and override this default."""
    monkeypatch.setenv("MAILBOX_CLEANUP_SOURCES", str(tmp_path / "sources.json"))


@pytest.fixture
def seeded_mailbox(fresh_mailbox):
    """Seed INBOX with all fixture .eml files."""
    g = fresh_mailbox
    eml_files = sorted(FIXTURE_DIR.glob("*.eml"))
    _seed_mailbox(g["host"], g["port"], g["user"], g["password"], eml_files)
    yield g


@pytest.fixture
def drafts_ready(fresh_mailbox):
    """Guarantee a drafts folder the resolver can find.

    Production code never creates it (spec §7.2). Only this test setup does,
    and only when GreenMail has none — see the PR body for the measurement.
    """
    from mailbox_cleanup.folders import resolve_folder

    g = fresh_mailbox
    with MailBoxUnencrypted(g["host"], port=g["port"]).login(g["user"], g["password"]) as mb:
        if resolve_folder(mb, "drafts") is None:
            mb.folder.create("Drafts")
            print("TEST SETUP: created 'Drafts' (GreenMail has no \\Drafts folder)")
        name = resolve_folder(mb, "drafts")
        mb.folder.set(name)
        uids = [m.uid for m in mb.fetch(mark_seen=False) if m.uid]
        if uids:
            mb.delete(uids)  # empty the drafts folder between tests
        mb.folder.set("INBOX")
    yield g
