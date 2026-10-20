"""IMAPS must authenticate the server: certificate and hostname verified, TLS 1.2+.

Before this fix, imap_connect() built MailBox(server, port) without an ssl_context, so
imaplib fell back to ssl._create_stdlib_context() -- CERT_NONE, no hostname check. A
network attacker could pose as the IMAP server and read the login password. Plaintext
IMAP was also reachable from any environment through MAILBOX_CLEANUP_SSL=0.
"""

import shutil
import socket
import ssl
import subprocess
import threading

import pytest

from mailbox_cleanup import imap_client
from mailbox_cleanup.auth import Credentials
from mailbox_cleanup.imap_client import (
    IMAPCertificateError,
    IMAPConnectionError,
    InsecureConnectionRefused,
    imap_connect,
)

REMOTE = Credentials(email="t@example.com", password="x", server="imap.example.com")
LOCAL = Credentials(email="t@example.com", password="x", server="127.0.0.1")


class _Recorder:
    """Stands in for MailBox / MailBoxUnencrypted and records how it was built."""

    built: list

    def __init__(self, server, port=993, **kwargs):
        type(self).built.append({"server": server, "port": port, **kwargs})

    def login(self, email, password):
        return self

    def logout(self):
        pass


@pytest.fixture
def boxes(monkeypatch):
    tls = type("TLSBox", (_Recorder,), {"built": []})
    plain = type("PlainBox", (_Recorder,), {"built": []})
    monkeypatch.setattr(imap_client, "MailBox", tls)
    monkeypatch.setattr(imap_client, "MailBoxUnencrypted", plain)
    monkeypatch.setattr(imap_client.time, "sleep", lambda s: None)
    return tls, plain


def test_tls_context_verifies_certificate_and_hostname(boxes, monkeypatch):
    monkeypatch.delenv("MAILBOX_CLEANUP_SSL", raising=False)
    tls, plain = boxes
    with imap_connect(REMOTE):
        pass
    assert plain.built == []
    ctx = tls.built[0].get("ssl_context")
    assert isinstance(ctx, ssl.SSLContext)
    assert ctx.verify_mode == ssl.CERT_REQUIRED
    assert ctx.check_hostname is True
    assert ctx.minimum_version >= ssl.TLSVersion.TLSv1_2


@pytest.mark.parametrize("ssl_flag", [None, False])
def test_plaintext_to_a_remote_server_is_refused(boxes, monkeypatch, ssl_flag):
    monkeypatch.setenv("MAILBOX_CLEANUP_SSL", "0")
    tls, plain = boxes
    with pytest.raises(InsecureConnectionRefused):
        with imap_connect(REMOTE, ssl=ssl_flag):
            pass
    assert plain.built == [] and tls.built == []


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1"])
def test_plaintext_to_loopback_is_allowed_for_tests(boxes, monkeypatch, host):
    monkeypatch.setenv("MAILBOX_CLEANUP_SSL", "0")
    tls, plain = boxes
    creds = Credentials(email="t@example.com", password="x", server=host)
    with imap_connect(creds):
        pass
    assert len(plain.built) == 1 and tls.built == []


def test_a_certificate_error_is_not_retried(monkeypatch):
    monkeypatch.delenv("MAILBOX_CLEANUP_SSL", raising=False)
    calls = []

    class _BadCert:
        def __init__(self, server, port=993, **kwargs):
            calls.append(1)
            raise ssl.SSLCertVerificationError("certificate verify failed")

    monkeypatch.setattr(imap_client, "MailBox", _BadCert)
    monkeypatch.setattr(imap_client.time, "sleep", lambda s: None)
    with pytest.raises(IMAPCertificateError) as exc:
        with imap_connect(REMOTE, max_retries=2):
            pass
    assert isinstance(exc.value, IMAPConnectionError)  # existing handlers keep working
    assert len(calls) == 1


# --- real handshake against a server with an untrusted certificate -------------------


@pytest.fixture
def self_signed_server(tmp_path):
    """A TLS listener on 127.0.0.1 with a self-signed certificate for 'localhost'.

    It completes the TLS handshake if the client accepts it and then speaks a minimal
    IMAP greeting, so a client that does NOT verify gets as far as the protocol.
    """
    if shutil.which("openssl") is None:
        pytest.skip("openssl binary not available")
    key, cert = tmp_path / "key.pem", tmp_path / "cert.pem"
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-days",
            "1",
            "-subj",
            "/CN=localhost",
            "-keyout",
            str(key),
            "-out",
            str(cert),
        ],
        check=True,
        capture_output=True,
    )
    server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_ctx.load_cert_chain(cert, key)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(4)
    sock.settimeout(5)
    handshakes = []

    def serve():
        for _ in range(4):
            try:
                conn, _ = sock.accept()
            except OSError:
                return
            try:
                with server_ctx.wrap_socket(conn, server_side=True) as tls:
                    handshakes.append(True)
                    tls.sendall(b"* OK IMAP4rev1 ready\r\n")
                    tls.recv(1024)
            except (ssl.SSLError, OSError):
                pass

    t = threading.Thread(target=serve, daemon=True)
    t.start()
    yield sock.getsockname()[1], handshakes
    sock.close()


def test_untrusted_certificate_is_refused_on_the_wire(self_signed_server, monkeypatch):
    monkeypatch.delenv("MAILBOX_CLEANUP_SSL", raising=False)
    monkeypatch.setattr(imap_client.time, "sleep", lambda s: None)
    port, handshakes = self_signed_server
    creds = Credentials(email="t@example.com", password="x", server="localhost")
    with pytest.raises(IMAPCertificateError):
        with imap_connect(creds, port=port, max_retries=0):
            pass
    assert handshakes == []  # the client aborted the handshake, no password went out
