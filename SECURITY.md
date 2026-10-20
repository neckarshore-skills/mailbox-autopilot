# Security Policy

## Supported Versions

| Version | Supported |
|---------|-----------|
| latest `main` | Yes |

This is a solo-maintained open-source tool. Only the current `main` receives
security fixes; there is no back-port of older tags.

## Data Handling

`mailbox-autopilot` connects to an IMAP mailbox to triage and delete messages.
Two points worth distinguishing:

**Credentials stay on your machine.** The tool reads IMAP host, username, and
password from your local environment/config. Credentials are used only to open
the IMAP connection you configured; they are never logged, transmitted to any
third party, or committed.

**The IMAP server must prove who it is (since 0.7.1).** The connection is IMAPS
with the server certificate and hostname verified against the system trust store,
TLS 1.2 or newer. A server whose certificate does not verify is refused, without
retry, as `connection_error`. Plaintext IMAP exists only for a loopback test server
(`MAILBOX_CLEANUP_SSL=0` with `127.0.0.1`/`localhost`); for any other host it is
refused.

**Audit log (since 0.8.0).** `~/.mailbox-cleanup/audit.log` is owner-only (file
0600, directory 0700; a log from an older version is tightened on the next run of
any command). It rotates at 5 MB and keeps five rotated files, so the oldest history
is eventually dropped. Records carry the *names* of the arguments given, never their
values: no addresses, subjects, bodies or keep-list entries. A move's target folder
is recorded, because it says where the mail went.

**Network egress.** Two kinds of outbound connection exist: (1) the IMAP server you
configure. There is no SMTP connection of any kind: a `mailto:` unsubscribe is never
executed, it is listed for you to handle by hand; (2) when you run
`unsubscribe`, an HTTP(S) request to the `List-Unsubscribe` URL published by the
sender. That URL is **email-controlled**, so the outbound request is gated by a
fail-closed SSRF guard (`validate_egress_url` in `operations/unsubscribe.py`):
non-`http(s)` schemes and any host resolving to a private / loopback /
link-local / reserved range (e.g. `127.0.0.1`, `169.254.169.254` cloud metadata,
RFC-1918) are refused before any request is sent, all resolved addresses are
checked (DNS-rebinding safe), and HTTP redirects are disabled so a permitted host
cannot bounce the request onto an internal target. No IMAP credentials are ever
attached to that unsubscribe request.

**No telemetry.** No analytics, error reporting, or auto-update calls are made.

For a mailbox you would not want a local script to read (privileged corporate
mail, legal, medical), apply your own judgement: the tool acts on whatever
mailbox you give it credentials for.

## Scope

The tool operates only on the IMAP account and folders you configure. Beyond its
own config and audit log it touches local files only where you name them: `manage`
reads a body file you pass with `--body-file` (from its outbox, see `manage outbox`),
and `manage save-attachment` writes one attachment to the file you name with `--out`,
never overwriting. Its only outbound calls are to the IMAP server you specify
and — during `unsubscribe` — to the sender's
`List-Unsubscribe` URL, which is guarded against SSRF as described under
**Data Handling → Network egress** above.

## Reporting a Vulnerability

Report security issues **privately** via GitHub's private vulnerability reporting:
the **Security** tab → **Report a vulnerability** on
<https://github.com/neckarshore-skills/mailbox-autopilot/security/advisories>.
This keeps the report confidential until a fix ships.

Include:

1. **What you found** — describe the vulnerability
2. **How to reproduce** — steps to trigger the issue
3. **Impact** — what could go wrong if exploited

**Response time:** Best-effort. This is a solo-maintained open-source project, not
a commercial service with an SLA.
