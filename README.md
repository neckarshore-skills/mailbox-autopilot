# mailbox-autopilot

<p align="center">
  <img src=".github/social-preview.jpg" alt="mailbox-autopilot — Mailbox Triage. Dry-run first." width="100%"/>
</p>

CLI plus Claude Code plugin for triaging and cleaning up an IMAP mailbox and drafting replies. Dry-run by default, audit-logged, soft-delete-only, never sends mail. Multi-account capable. Tested on IONOS; the server defaults to `imap.ionos.de` and `--server` takes any IMAP host.

[![CI](https://github.com/neckarshore-skills/mailbox-autopilot/actions/workflows/ci.yml/badge.svg)](https://github.com/neckarshore-skills/mailbox-autopilot/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

---

## What it does

Battle-tested in one production session: 7.982 → 690 messages (-91%) on a real IONOS mailbox.

Two pieces:

1. **CLI** (Python, [`click`](https://click.palletsprojects.com), [`imap-tools`](https://github.com/ikvk/imap_tools)) — atomic subcommands with JSON output. Stateless. Testable.
2. **Claude Code Skill** — conversational orchestrator that wraps the CLI in a discovery → preview → apply loop. Asks before every destructive action.

The CLI is useful on its own. The Skill turns it into a guided triage workflow.

## Why hybrid

- **Pure CLI** would force you to memorize subcommands and read JSON.
- **Pure Skill** would push IMAP logic into Markdown / tool calls — fragile, slow, untestable.
- **Hybrid** keeps the engine testable (`pytest` against a real IMAP server in Docker) and the UX conversational.

## Architecture

```
Claude Code Session
  ↓ /mailbox-autopilot:cleanup or natural request
Claude Skill (Markdown, orchestrator)
  ↓ subprocess + JSON
CLI: mailbox-autopilot <subcommand> [--account=<alias>] [--apply | --json]
  ↓ imap-tools
IMAP server (default: IONOS)
```

State files (per user):

| Path | Purpose | Mode |
|------|---------|------|
| `~/.mailbox-cleanup/config.json` | Identity + connection settings per account (source of truth) | 0600 |
| macOS Keychain (service `mailbox-cleanup`) | Passwords only — one entry per email | OS-managed |
| `~/.mailbox-cleanup/audit.log` | JSONL forensics, includes `account` field per record; rotates at 5 MB, five rotated files kept (since 0.8.0) | 0600 (directory 0700) |

## Safety model

| Layer | Mechanism |
|-------|-----------|
| **Default** | Every destructive subcommand is dry-run. `--apply` is required to actually do anything. |
| **Soft-delete** | `delete` moves to `Papierkorb` / `Trash` (resolved via RFC 6154 SPECIAL-USE flag with literal fallbacks). No `EXPUNGE` in v1. |
| **Skill flow** | Always shows a preview from a dry-run before re-running with `--apply`. Asks for explicit confirmation. |
| **Audit log** | Every `--apply` action appends one JSON-line to `~/.mailbox-cleanup/audit.log` with timestamp, account, the names of the arguments given (never their values), folder, affected UIDs, and result. Owner-only (0600). |
| **Credentials** | macOS Keychain via [`keyring`](https://github.com/jaraco/keyring). No `.env`, no plaintext. |
| **Final step** | True deletion is manual — empty `Papierkorb` in IONOS Webmail. |

## Install

Requires Python 3.11+ and [`uv`](https://docs.astral.sh/uv/) (`brew install uv`).

```bash
git clone https://github.com/neckarshore-skills/mailbox-autopilot.git
cd mailbox-autopilot
uv tool install --editable .
```

This puts `mailbox-autopilot` on your `PATH` (typically `~/.local/bin/mailbox-autopilot`). `mailbox-cleanup`, the CLI's earlier name, is installed next to it as an alias and runs the same code, so existing scripts keep working. What does not change: the `~/.mailbox-cleanup/` directory, the Keychain service `mailbox-cleanup` and the `MAILBOX_CLEANUP_*` environment variables keep their names, so an existing setup carries over untouched.

## Setup

`auth set` requires a real terminal (Terminal.app / iTerm — `getpass` requires a TTY).

### First-time setup (one account)

```bash
mailbox-autopilot auth set --alias=work --email=you@example.com
mailbox-autopilot auth test --account=work
```

`auth set` writes the account record to `~/.mailbox-cleanup/config.json` and stores the password in the macOS Keychain (service `mailbox-cleanup`, account = email).

### Adding a second account

```bash
mailbox-autopilot auth set --alias=private --email=other@example.com
mailbox-autopilot config list
mailbox-autopilot config set-default work
```

### Migrating from v0.1

> Existing v0.1 users: run any subcommand once with `--email=<your-email>` and the CLI auto-creates `~/.mailbox-cleanup/config.json` with a derived alias. After that, `--account=<alias>` is the preferred flag.

### Claude Code plugin: `mailbox-autopilot`

The repository ships as a Claude Code plugin with two skills, `cleanup` (triage, archive, delete, unsubscribe) and `manage` (search, read, follow threads, draft replies and new mail into your Drafts folder), plus a send-blocking hook. The plugin never sends mail: a reply or a new mail lands in Drafts and you send it from your own mail client.

Install from the Neckarshore marketplace:

```bash
/plugin marketplace add neckarshore-skills/neckarshore-plugins
/plugin install mailbox-autopilot@neckarshore-ai
```

- **A new mail:** `manage compose --to <address> --subject "<subject>" --body-file <file>` writes a mail that answers nothing into Drafts. To and Cc only (repeat the option, at most 10 addresses together, each one bare address). No Bcc, no attachment and no forwarding: those are refused with `out_of_scope`. `manage search --recipient <name>` matches the To header, so the skill can look up whom you have written to before; it shows you the address and waits for your yes.
- **A reply goes to everyone (since 0.6.0):** `manage draft` answers the sender (or its Reply-To) and the other To recipients in To and keeps the Cc in Cc, without your own address. `--preview` shows those recipients and writes nothing; `--sender-only` answers the sender alone; `--drop <address>` leaves one out and cannot add anyone. More than 10 recipients is refused, not cut short. Up to 0.5.0 a reply went to the sender only.
- **The answered mail is quoted below the reply (since 0.7.0):** `--quote-lang de|en` (the language of the reply) puts it under a line like "Am … schrieb …", each line prefixed with `>`; `--no-quote` leaves it out. A write needs one of the two: there is no default language.
- **The outbox:** `manage draft` and `manage compose` read their text from a file directly in `~/.mailbox-cleanup/outbox/` only (at most 1 MB, UTF-8, no symlink; `manage outbox` creates the folder and prints its path). A draft has a recipient, and a command that reads any file would let one deceived agent command stage a local file as a draft. This is a change for `manage draft`, which read any text file up to 0.3.4. It removes the one-command path; it does not stop a file that was first copied into the outbox.
- **Attachments:** `manage read` lists a mail's attachments, and `manage save-attachment --uid <UID> --index <N> --out <path>` writes one to a path you name. It never overwrites a file, never writes hidden files, under `~/Library` or to a file name that is loaded automatically (such as `CLAUDE.md` or `conftest.py`), writes only inert formats (documents, spreadsheets, calendar files and images; no archive, script, plain-text, Markdown, JSON or macro file, and no name without an extension), and changes nothing in the mailbox.
- **Prerequisite:** `uv` on your PATH. The plugin's `bin/mailbox-autopilot` launcher runs the CLI through `uv` from the plugin folder; without `uv` it stops with exit 127 and says so.
- **The first call is slow and needs network.** `uv` builds the environment inside the plugin folder and may download Python 3.11+. That can take several seconds with no output; later calls are fast.
- **Tested in Claude Code only.** The plugin depends on its `bin/` launcher and a local `uv`, so it needs a surface with a local shell.
- **Where the command works:** inside Claude Code, the plugin puts `mailbox-autopilot` on PATH. Your own terminal does not get it. For `auth set` (which needs a real terminal) use the CLI install above, or ask Claude for the launcher's absolute path.
- **The send-blocking hook** is a PreToolUse hook on Bash. It blocks the send routes it recognises (`smtplib`/`sendmail` in scripts, `osascript` telling Mail to send, `curl` to `smtp://`/`smtps://`) and passes ordinary commands. It is a partial second lock: an obfuscated command can get past it. The first lock is that the package contains no send code.
- **Migrating from the old skill:** if you symlinked or copied `skill/` to `~/.claude/skills/mailbox-cleanup`, remove that entry **before** installing the plugin (`rm ~/.claude/skills/mailbox-cleanup` for a symlink). The skill now lives in `skills/cleanup/`; keeping the old entry would load it twice, and a symlink into a clone breaks as soon as that clone is updated.

## Multi-account

Once two or more accounts are configured, every subcommand picks an account via this resolution order:

1. Explicit `--account=<alias>` or `--account=<email>`
2. Environment variable `MAILBOX_CLEANUP_ACCOUNT`
3. Configured default (`config set-default <alias>`)
4. The single account, if only one is configured
5. Otherwise: error `no_account_selected` (exit 4)

```bash
# Operate on the default account
mailbox-autopilot scan

# Operate on a specific account
mailbox-autopilot scan --account=private
mailbox-autopilot scan --account=other@example.com   # email also works

# Override via env var (useful for scripts/Marvin cron)
MAILBOX_CLEANUP_ACCOUNT=private mailbox-autopilot scan

# Manage accounts
mailbox-autopilot config list                # tabular
mailbox-autopilot config list --json         # machine-readable
mailbox-autopilot config show work
mailbox-autopilot config rename work office
mailbox-autopilot config set-default office
mailbox-autopilot config remove private      # keeps the Keychain password (since 0.8.0)
mailbox-autopilot config remove private --delete-credentials   # asks, then deletes it too
```

## Usage

### From Claude Code

```
/mailbox-autopilot:cleanup
/mailbox-autopilot:manage
```

Or ask in plain words ("räum mein Postfach auf", "antworte auf die Mail von X"). The cleanup skill runs `auth test`, then `scan`, presents a German-language category summary, and prompts for action per category. Always shows a dry-run preview before any `--apply`.

### Standalone CLI

```bash
# Discovery
mailbox-autopilot scan --account=work --json
mailbox-autopilot senders --account=work --top 50

# Dry-run delete (preview only)
mailbox-autopilot delete --account=work --sender "newsletter@example.com"

# Apply
mailbox-autopilot delete --account=work --sender "newsletter@example.com" --apply

# Combine filters (AND)
mailbox-autopilot delete \
  --account=work \
  --sender "noreply@github.com" \
  --older-than 6m \
  --apply

# Newsletters older than 2 days, except two senders you keep.
# The dry-run reports affected_count, kept_count and a by_sender breakdown.
mailbox-autopilot delete --account=work --category newsletter --older-than 2d \
  --keep "billing@example.com" --keep "@example.org"

# Apply only the set you confirmed: refuses (preview_mismatch) if it changed
mailbox-autopilot delete --account=work --category newsletter --older-than 2d \
  --keep "billing@example.com" --keep "@example.org" --apply --expect-count 1388

# Several senders at once, or everything sent to one address
mailbox-autopilot delete --account=work --sender "a@example.com" --sender "b@example.com"
mailbox-autopilot delete --account=work --recipient "alias@example.net"

# Move (e.g. invoices to a tax folder)
mailbox-autopilot move \
  --account=work \
  --sender "noreply@ionos.de" \
  --to "STEUER Rechnungen Finanzamt" \
  --apply

# Bulk archive
mailbox-autopilot archive --account=work --older-than 12m --apply

# Unsubscribe (RFC 2369 / RFC 8058 one-click)
mailbox-autopilot unsubscribe --account=work --sender "newsletter@example.com" --apply

# Dedupe by Message-ID (keeps oldest)
mailbox-autopilot dedupe --account=work --apply

# Find bounce / auto-reply
mailbox-autopilot bounces --account=work --apply

# List large attachments (strip = v2)
mailbox-autopilot attachments --account=work --size-gt 10mb
```

If only one account is configured, `--account` can be omitted.

## Subcommands

| Subcommand | Purpose | Required args | Dry-run by default |
|------------|---------|---------------|---------------------|
| `auth set` | Create/update account in config.json + write password to Keychain | `--alias=`, `--email=` (interactive password) | n/a |
| `auth test` | Connect, list folders, disconnect | — (uses default/`--account`) | n/a |
| `auth delete` | Remove credentials from Keychain | `--account=` | n/a |
| `config list` | List configured accounts | — | n/a (read-only) |
| `config show` | Show one account's connection settings | `<alias>` | n/a (read-only) |
| `config rename` | Rename an alias | `<old>`, `<new>` | n/a |
| `config set-default` | Mark an account as default | `<alias>` | n/a |
| `config remove` | Delete account from config; the Keychain password stays unless `--delete-credentials` is given and confirmed (since 0.8.0; refused with `--non-interactive`) | `<alias>` | n/a |
| `config keep add` / `remove` / `list` | Keep list of one account: senders that `delete`, `move`, `unsubscribe` and `bounces` never touch (since 0.5.0) | entries (address or `@domain`); optional `--account=` | n/a |
| `scan` | Discovery — classify INBOX, return JSON report | `--folder=INBOX` (default) | n/a (read-only) |
| `senders` | List top-N senders by count | `--top=50` | n/a (read-only) |
| `delete` | Soft-delete (move to Trash) by filter | one of `--sender=` (repeatable) / `--subject-contains=` / `--older-than=` / `--recipient=` / `--category=`; optional `--keep=` (repeatable), `--expect-count=` | yes |
| `move` | Move by filter to target folder | `--to=Folder` plus the same filters as `delete` | yes |
| `archive` | Bulk-move messages older than N → `Archive/YYYY` | `--older-than=12m` | yes |
| `unsubscribe` | Parse `List-Unsubscribe` header, execute HTTPS one-click only; `mailto:`-only senders are listed under `manual_unsubscribe` and their mail is kept (the package sends no mail). If the HTTPS unsubscribe fails (since 0.4.1), the mail is kept as well, the command exits 5 with `unsubscribe_failed`, and the failed link plus any `mailto:` alternative come back under `manual_unsubscribe`; a link the egress guard refused as unsafe comes back under `refused_unsubscribe` instead and is never offered for manual handling | `--sender=` | yes |
| `dedupe` | Drop Message-ID duplicates, keep oldest | `--folder=` | yes |
| `attachments` | List large messages (v1) — strip is v2 | `--size-gt=10mb` | n/a (read-only v1) |
| `bounces` | Find bounce / auto-reply messages | `--folder=INBOX` | yes |

**Common flags:** `--account=<alias|email>` (account selector), `--json` (structured output), `--apply` (execute, default off), `--folder=` (target IMAP folder), `--limit=N` (cap operation size).

**Time syntax for `--older-than`:** `Nd` / `Nw` / `Nm` / `Ny` (days / weeks / months / years).

**Filter combinability:** `delete --account=work --sender=X --older-than=3m --apply` (AND across filters; several `--sender` values are OR'ed among themselves).

**Categories (`--category`):** `newsletter`, `automated`, `bounce` — the same classifier `scan` uses, applied to the fetched headers. `--limit` counts after the category and keep-list filters.

**Keep-list (`--keep`):** a full address, or a domain written `@example.com` (its subdomains are kept too). Bare words are refused: a substring match on a name would keep things nobody can predict.

**Persistent keep list (since 0.5.0):** each account can carry its own keep list in `config.json`, maintained with `config keep add|remove|list [--account=<alias>]`. `delete`, `move`, `unsubscribe` and `bounces` on that account leave mail from those senders alone; for `delete` and `move` the list adds to any `--keep` flags. The dry-run shows `kept_by_config` (how many of `kept_count` the account's list kept) and `keep_config` (the list itself), and the audit log records it under `args.keep_config`. `dedupe` always keeps one copy and `archive` only moves within the mailbox, so neither consults the list. An entry that is neither an address nor `@domain` makes every command stop before connecting with `bad_config` (exit 4). A CLI older than 0.5.0 ignores the field when reading and drops it when one of its `config` or `auth set` commands rewrites the file, and it does not apply the list on `delete`, `move`, `unsubscribe` or `bounces` either. Add the first entry only after the CLI the skill runs on that machine reports 0.5.0 (`mailbox-autopilot --version`).

**Preview binding (`--expect-count`):** with `--apply`, pass the `affected_count` the dry-run showed. If the matching set changed size in between, nothing moves and the CLI exits 4 with `preview_mismatch`.

**Large moves** run in batches of 500. If a batch fails, the CLI exits 5 with `partial_failure`, reports `moved_count` and `not_moved_count`, and the audit record lists exactly the UIDs that moved.

## Discovery report (`scan --json`)

The contract between CLI and Skill — `scan` always emits this shape:

```json
{
  "schema_version": 1,
  "scanned_at": "2026-05-04T...",
  "folder": "INBOX",
  "total_messages": 7982,
  "size_total_mb": 65.5,
  "categories": {
    "newsletters": {"count": 5649, "top_senders": []},
    "automated_notifications": {"count": 652, "top_senders": []},
    "bounces_and_autoreplies": {"count": 1, "samples": []},
    "large_attachments": {"count": 0, "size_mb": 0, "top_offenders": []},
    "duplicates": {"count": 18, "groups": []},
    "old_messages": {"older_than_12m": 196},
    "by_year": {"2024": 73, "2025": 887, "2026": 7022}
  },
  "recommendations": ["...", "..."]
}
```

`schema_version` is checked by the Skill — version mismatch means update one side before continuing.

### Classification rules

| Category | Rule |
|----------|------|
| **newsletter** | `List-Unsubscribe` header present **OR** sender local-part matches `newsletter`, `news`, `marketing`. A `noreply` / `no-reply` sender alone is **not** a newsletter (since 0.3.2): login alerts, invoices and tickets come from such addresses. Notification mail that carries `List-Unsubscribe` still matches, so check `by_sender` in the dry-run and use `--keep` |
| **automated** | sender local-part matches `notifications`, `bot`, `service`, `alerts`, `system`, `daemon`, `automation` |
| **bounce** | sender is `MAILER-DAEMON` / `postmaster` **OR** subject starts with `Undelivered`, `Returned`, `Mail Delivery`, `Auto-Reply`, `Out of Office`, `Abwesenheits` |
| **duplicate** | identical `Message-ID` header (true dupe; fuzzy dedupe deferred to v2) |
| **large_attachment** | message size > 10 MB |

A message can fall into multiple categories.

## Audit log

Path: `~/.mailbox-cleanup/audit.log` (override with `MAILBOX_CLEANUP_AUDIT_LOG`).

Format: one JSON object per line. The `account` field identifies which alias performed the action (optional for backward compatibility with v0.1 entries that pre-date multi-account). Example:

```json
{"timestamp":"2026-10-20T09:27:45.504Z","subcommand":"delete","account":"work","arg_keys":["older_than","sender"],"folder":"INBOX","affected_uids":["655773","672257"],"result":"success","keep_config_count":2}
```

Fields: `timestamp` (UTC, ISO 8601), `account`, `subcommand` (a CLI subcommand name), `arg_keys` (since 0.8.0 for every record: the sorted names of the arguments given, never their values; records before 0.8.0 carry `args` with the values), `keep_config_count` (the length of the account's keep list, not its entries), `target_folder` (`move` only: where the mail went), `folder`, `affected_uids`, `result` — always a string: `success`, `partial_failure` or `error`; `unsubscribe` also writes `manual` (mailto-only, mail kept), `failed` (the HTTPS unsubscribe failed, mail kept) and `kept` (since 0.5.0: every matching message is from a sender on the account's keep list, nothing done), all with empty `affected_uids` — and `error` (an error code) when the action did not fully succeed.

Only the CLI writes this file. Read-only ad-hoc scripts do not log, and nothing else may append to it.

Rotation (since 0.8.0): at 5 MB the live file becomes `audit.log.1`, older copies move up to `audit.log.5`, and the oldest is dropped. The `jq` example below reads the live file only; add `audit.log.*` to include the rotated ones.

Inspect with `jq`:

```bash
# Group by account + subcommand
jq -s 'group_by(.account + "/" + .subcommand) | map({op: (.[0].account + "/" + .[0].subcommand), count: (map(.affected_uids|length)|add)})' ~/.mailbox-cleanup/audit.log
```

## Exit codes and error codes

The CLI exits with a numeric code; structured errors include a stable `error` string in the JSON payload (when `--json` is used).

| Code | Meaning | Exit |
|------|---------|------|
| `auth_missing` | No password in Keychain for the resolved account | 3 |
| `connection_error` | IMAP connection / TLS / network failure | 2 |
| `no_account_selected` | Multiple accounts; no default; no `--account` or env var | 4 |
| `unknown_account` | `--account=foo` matched neither alias nor email | 4 |
| `confirmation_required` | `config remove --delete-credentials --non-interactive`: deleting a Keychain password needs an interactive confirmation; nothing was changed (since 0.8.0) | 4 |
| `duplicate_alias` | `auth set --alias=X` but X exists | 4 |
| `duplicate_email` | `auth set --email=X` but X exists | 4 |
| `bootstrap_failed` | Auto-migration from v0.1 failed | 4 |
| `no_config` | No config file and no v0.1 fallback | 5 |
| `config_corrupt` | Existing JSON parse failure | 5 |
| `schema_version_unsupported` | Config from a future version | 5 |
| `unsubscribe_failed` | `unsubscribe --apply`: the HTTPS unsubscribe failed; no mail was moved (since 0.4.1) | 5 |
| `bad_config` | The config file cannot be used (for example a keep entry that is neither an address nor `@domain`); nothing connects (since 0.5.0) | 4 |
| `not_in_keep_list` | `config keep remove`: the entry is not on that account's list; nothing changed (since 0.5.0) | 4 |
| `move_unsupported` | `delete` / `move --apply`: the server offers neither IMAP MOVE nor UIDPLUS, so a move could remove messages outside the selection; nothing was moved (since 0.7.2) | 5 |

Generic exit codes:

| Code | Meaning |
|------|---------|
| 0 | Success |
| 2 | Connection error |
| 3 | Auth missing |
| 4 | Bad arguments / account resolution |
| 5 | Partial failure / config error |

## Repo layout

```
mailbox-autopilot/
├── README.md                              ← you are here
├── pyproject.toml                         ← Python 3.11+, click, imap-tools, keyring, requests, pytest, ruff
├── src/mailbox_cleanup/
│   ├── __init__.py                        ← __version__, SCHEMA_VERSION
│   ├── cli.py                             ← click entry point
│   ├── auth.py                            ← Keychain
│   ├── config.py                          ← config.json schema + I/O
│   ├── imap_client.py                     ← imap-tools wrapper, retry, SSL toggle
│   ├── classify.py                        ← pure-function classification rules
│   ├── scan.py                            ← discovery → JSON report
│   ├── folders.py                         ← SPECIAL-USE folder resolver
│   ├── audit.py                           ← JSONL audit log writer (account-aware)
│   └── operations/
│       ├── filters.py
│       ├── delete.py
│       ├── move.py
│       ├── archive.py
│       ├── unsubscribe.py
│       ├── dedupe.py
│       ├── attachments.py
│       └── bounces.py
├── tests/                                 ← unit + integration via Greenmail Docker
├── docs/
│   ├── 2026-05-04-design.md                       ← v0.1 spec
│   ├── 2026-05-04-implementation-plan.md           ← v0.1 TDD plan
│   ├── 2026-05-04-multi-account-design.md          ← v0.2 spec
│   ├── 2026-05-04-multi-account-implementation-plan.md  ← v0.2 TDD plan
│   └── smoke-test.md                               ← read-only IONOS smoke test
├── .claude-plugin/plugin.json             ← Claude Code plugin manifest
├── bin/mailbox-autopilot                  ← plugin launcher (runs the CLI via uv)
├── hooks/                                 ← send-blocking PreToolUse hook
├── skills/cleanup/SKILL.md                ← cleanup skill
├── skills/manage/SKILL.md                 ← manage skill (search, read, draft)
└── .github/workflows/ci.yml               ← GitHub Actions
```

## Tests

```bash
uv sync --extra dev
uv run pytest -v               # Greenmail Docker auto-starts via conftest
uv run ruff check .
uv run ruff format --check .
```

CI runs the same on every push. Greenmail starts on port 3143 (plain IMAP) + 3025 (SMTP).

> **Note:** a bare `uv run pytest` starts the Greenmail container when Docker is running. To stay without Docker, run `uv run pytest -m "not integration"`: the unit tests pass, and nine older CLI tests that are not marked as integration report an error because they need the container. CI runs everything.

## Estate test-scope stats

This repo is a **producer** for the neckarshore.ai estate test-count. On every `push:main`, CI counts the two gated pytest suites (unit + the live-Greenmail integration suite) from pytest's own `--collect-only` reporter — never grep — and publishes a contract-valid `stats.json` to the dedicated [`stats-data`](../../tree/stats-data/stats.json) branch: a single-file data branch, **not** `main`. `main` is a protected branch (a bot cannot push to it without weakening its protection), so the machine artifact lives on its own unprotected branch instead. The neckarshore.ai aggregator fetches it via `contents/stats.json?ref=stats-data`. Contract: [`stats-json-contract.md`](https://github.com/neckarshore-ai/neckarshore-planning/blob/main/docs/reference/stats-json-contract.md).

## Limitations (v0.4)

1. Tested against IONOS and the GreenMail test server only. `--server` and `--port` take any IMAP host and folders are found by their standard special-use flags, but no other provider has been run. No Gmail API or Office365 support
2. Strict dedupe — only by exact `Message-ID`; fuzzy hash = v2
3. Attachment listing only — in-place strip = v2
4. No hard-delete — final step is manual `Papierkorb leeren` in IONOS Webmail
5. Rule-based classification only — no ML / LLM in CLI (Skill can layer it on)
6. `auth set` requires a real TTY — no `--password-stdin` yet (v2)

## Backlog

- Provider abstraction (Gmail API / OAuth)
- Fuzzy duplicate detection
- Attachment strip (append stripped + delete original)
- Marvin cron integration for autonomous background cleanup
- Web UI / TUI
- `--password-stdin` for non-TTY setup
- `purge-trash` hard-delete subcommand

## References

- IMAP RFC 3501, RFC 6154 (SPECIAL-USE), RFC 2369 (List-Unsubscribe), RFC 8058 (One-Click POST)
- [imap-tools](https://github.com/ikvk/imap_tools)
- [click](https://click.palletsprojects.com)
- [keyring](https://github.com/jaraco/keyring)
- [Greenmail](https://greenmail-mail-test.github.io/greenmail/) — test IMAP server

## License

[MIT](LICENSE) — feel free to fork, modify, redistribute. No warranty; built primarily for the author's own IONOS mailbox.

## Author

German Rauhut · `german@rauhut.com`
