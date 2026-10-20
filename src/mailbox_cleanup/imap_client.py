import os
import time
from contextlib import contextmanager

from imap_tools import MailBox, MailBoxUnencrypted

from .auth import Credentials

SSL_ENV = "MAILBOX_CLEANUP_SSL"  # set to "0" to disable SSL (used by Greenmail tests)


def _refresh_capabilities(mb) -> None:
    """Replace the pre-login capability list with the one the server sends after login.

    imaplib keeps what the greeting announced; some servers (Hostinger) add MOVE and
    UIDPLUS only after authentication, and imap_tools decides on MOVE from that list.
    On failure the old list stays, and batching refuses a move it cannot scope (#73).
    """
    client = getattr(mb, "client", None)
    if client is None:
        return
    try:
        typ, data = client.capability()
    except Exception:
        return
    if typ == "OK" and data and data[-1]:
        raw = data[-1].decode() if isinstance(data[-1], bytes) else str(data[-1])
        client.capabilities = tuple(raw.upper().split())


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
    # Only connect + login are retried. The body runs once: an exception from it must
    # reach the caller unchanged, never be caught here and "retried" by yielding again.
    for attempt in range(max_retries + 1):
        try:
            mb = Cls(creds.server, port=port).login(creds.email, creds.password)
            break
        except Exception as e:
            if attempt < max_retries:
                time.sleep(2 ** (attempt + 1))
                continue
            raise IMAPConnectionError(
                f"Failed to connect to {creds.server}:{port} as {creds.email}: {e}"
            ) from e
    _refresh_capabilities(mb)
    try:
        yield mb
    finally:
        mb.logout()
