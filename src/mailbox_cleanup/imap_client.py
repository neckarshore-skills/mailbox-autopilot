import ipaddress
import os
import ssl as _ssl
import time
from contextlib import contextmanager

from imap_tools import MailBox, MailBoxUnencrypted

from .auth import Credentials

# Set to "0" to use plaintext IMAP. Honoured ONLY for a loopback server (the Greenmail
# test container); for any other host the connection is refused.
SSL_ENV = "MAILBOX_CLEANUP_SSL"

_LOOPBACK_NAMES = ("localhost",)


class IMAPConnectionError(Exception):
    pass


class IMAPCertificateError(IMAPConnectionError):
    """The server's TLS certificate or hostname did not verify. Never retried."""


class InsecureConnectionRefused(IMAPConnectionError):
    """Plaintext IMAP was requested for a host that is not loopback."""


def _is_loopback(host: str) -> bool:
    if host.lower() in _LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def tls_context() -> _ssl.SSLContext:
    """Certificate and hostname verified against the system trust store, TLS 1.2 or newer.

    imaplib's own fallback (ssl._create_stdlib_context) verifies nothing, so this context
    must always be passed explicitly.
    """
    ctx = _ssl.create_default_context()
    ctx.minimum_version = _ssl.TLSVersion.TLSv1_2
    return ctx


def _open(creds: Credentials, port: int, use_tls: bool):
    if use_tls:
        return MailBox(creds.server, port=port, ssl_context=tls_context())
    return MailBoxUnencrypted(creds.server, port=port)


@contextmanager
def imap_connect(
    creds: Credentials,
    *,
    port: int = 993,
    ssl: bool | None = None,
    max_retries: int = 2,
):
    """Connect to IMAP with single retry on transient failures.

    Production: IMAPS with a verifying TLS context. Plaintext (ssl=False or env
    MAILBOX_CLEANUP_SSL=0) is allowed only for a loopback server and refused otherwise.
    A certificate failure is not transient and is raised at once, without retry.
    Backoff: 2s, 4s. After max_retries exhausted, raises IMAPConnectionError.
    """
    if ssl is None:
        ssl = os.environ.get(SSL_ENV, "1") != "0"
    if not ssl and not _is_loopback(creds.server):
        raise InsecureConnectionRefused(
            f"Plaintext IMAP to {creds.server} refused: only a loopback server may be "
            f"reached without TLS (unset {SSL_ENV})."
        )
    # Only connect + login are retried. The body runs once: an exception from it must
    # reach the caller unchanged, never be caught here and "retried" by yielding again.
    for attempt in range(max_retries + 1):
        try:
            mb = _open(creds, port, ssl).login(creds.email, creds.password)
            break
        except _ssl.SSLCertVerificationError as e:
            raise IMAPCertificateError(
                f"TLS certificate of {creds.server}:{port} did not verify: {e}"
            ) from e
        except Exception as e:
            if attempt < max_retries:
                time.sleep(2 ** (attempt + 1))
                continue
            raise IMAPConnectionError(
                f"Failed to connect to {creds.server}:{port} as {creds.email}: {e}"
            ) from e
    try:
        yield mb
    finally:
        mb.logout()
