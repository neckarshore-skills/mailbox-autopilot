---
name: cleanup
description: Discover and clean up an IMAP mailbox via the `mailbox-autopilot` CLI. Use when the user wants to triage, scan, delete, archive, or unsubscribe from messages in their mail account, or asks which mail accounts are configured. Always shows dry-run preview before any destructive operation. Multi-account capable. For finding, reading or answering mail use the manage skill.
---

# cleanup (mailbox-autopilot)

Conversational orchestrator over the `mailbox-autopilot` CLI (`mailbox-cleanup` remains an alias of the same CLI). Wraps discovery → preview → apply loops with safety checks. Multi-account capable: every CLI call resolves to one account via `--account=<alias|email>` or the configured default.

## Required CLI version

Schema version 1. The CLI emits `"schema_version": 1` in every JSON response — if it doesn't match, abort and tell the user to update the CLI.

## Setup state — detect first, every session

Before anything else, find out which accounts are configured:

```bash
mailbox-autopilot config list --json
```

Read the `accounts` array from the response. Three states:

1. **Empty / file missing** — no accounts yet. Run the setup-time decision tree below.
2. **One account** — use it implicitly. No `--account` flag needed.
3. **Multiple accounts** — pick one (see "Picking an account" below) and pass `--account=<alias>` to every subsequent CLI call.

### Setup-time decision tree

```
if config.json missing AND user has v0.1 keychain entry:
  run any subcommand with --email=<their email> → triggers auto-bootstrap
  OR run `config init --import-email=<email>` explicitly
if config.json missing AND no v0.1 entry:
  ask user for alias + email, then guide them to run in a real terminal (absolute launcher path, see below):
    mailbox-autopilot auth set --alias=<alias> --email=<email>
  (auth set needs a TTY for getpass — Claude Code cannot run it interactively)
if config.json exists:
  use accounts as listed; ask user which one if ambiguous
```

### Commands the user runs in their own terminal

`mailbox-autopilot` is on PATH only inside Claude Code, where the plugin adds its `bin/` folder. The user's own terminal does not know the command. Before you hand the user a command such as `auth set`, run `command -v mailbox-autopilot` yourself and give them the absolute path it prints, for example `/Users/<name>/.claude/plugins/.../bin/mailbox-autopilot auth set --alias=<alias> --email=<email>`. Never tell them to type the bare command name.

## Picking an account

Once `config list --json` returns a non-empty `accounts` array:

1. **One account** — use it. Don't ask. Don't pass `--account` (the CLI resolves the single account automatically).
2. **Multiple accounts, default set** — assume the default unless the user says otherwise. You may briefly note it: "Ich nutze den Default-Account `<alias>`. Anderer Account?"
3. **Multiple accounts, no default** — ask: "Welcher Account: `work`, `private`, ...?" Then pass `--account=<chosen>` to every subsequent command in the session.

Store the chosen alias in your working memory for the session. Substitute `<ACCOUNT>` in the commands below with the chosen alias (or omit `--account=<ACCOUNT>` entirely when there is exactly one configured account).

## Auth check (run after picking the account)

```bash
mailbox-autopilot auth test --account=<ACCOUNT> --json
```

- Exit 0 with `"ok": true`: continue.
- Exit 3 (`auth_missing`): tell the user to run `auth set --alias=<ACCOUNT> --email=<their email>` in a real terminal, with the absolute launcher path from "Commands the user runs in their own terminal" (Terminal.app / iTerm — `getpass` requires a TTY). Do not proceed.
- Exit 4 (`no_account_selected` / `unknown_account`): re-check the account list; you may have a stale alias.
- Exit 2 (connection): show the message; do not retry blindly.

## Standard flow

1. Run `mailbox-autopilot scan --account=<ACCOUNT> --json`.
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
   - Only on explicit confirmation, run again with `--apply`. For `delete` and `move`, add
     `--expect-count <affected_count from the dry-run>`; if the CLI answers `preview_mismatch`,
     the mailbox changed since the preview: show the new dry-run and ask again. The other
     subcommands (`archive`, `bounces`, `dedupe`, `unsubscribe`) do not accept the flag yet.
6. After `--apply`, show the result count and tell the user the audit log is at `~/.mailbox-cleanup/audit.log`.
7. Loop back to step 4 for the next category.

## Subcommand cheat sheet

Replace `<ACCOUNT>` with the chosen alias (or omit the `--account` flag when only one account is configured).

| User intent | Command |
|-------------|---------|
| "Welche Accounts?" | `mailbox-autopilot config list --json` |
| "Scan" / "Was ist drin?" | `mailbox-autopilot scan --account=<ACCOUNT> --json` |
| "Wer schickt am meisten?" | `mailbox-autopilot senders --account=<ACCOUNT> --top 20 --json` |
| "Lösch alles von X" | `mailbox-autopilot delete --account=<ACCOUNT> --sender X --json` (then `--apply`) |
| "Alle Newsletter älter als 2 Tage, außer X und Y" | `mailbox-autopilot delete --account=<ACCOUNT> --category newsletter --older-than 2d --keep x@example.com --keep @example.org --json` |
| "Diese Absender weg" (several) | `mailbox-autopilot delete --account=<ACCOUNT> --sender a@example.com --sender b@example.com --json` |
| "Alles an Adresse X" | `mailbox-autopilot delete --account=<ACCOUNT> --recipient x@example.com --json` |
| "Alle Newsletter ins Archiv" | `mailbox-autopilot move --account=<ACCOUNT> --to Archiv --category newsletter --json` |
| "Alles älter als 1 Jahr archivieren" | `mailbox-autopilot archive --account=<ACCOUNT> --older-than 12m --json` |
| "Vom Newsletter X abmelden" | `mailbox-autopilot unsubscribe --account=<ACCOUNT> --sender X --json` (HTTPS one-click only; `mailto:`-only senders come back under `manual_unsubscribe` for the user to unsubscribe by hand, their mail is kept; if the HTTPS unsubscribe fails, the command exits 5 (`unsubscribe_failed`), the mail is kept, and the failed link plus any `mailto:` alternative come back under `manual_unsubscribe` — tell the user it failed, never that it worked. Targets under `refused_unsubscribe` were refused as unsafe: name them as refused and never suggest opening them) |
| "Bounces wegräumen" | `mailbox-autopilot bounces --account=<ACCOUNT> --json` |
| "Duplikate finden" | `mailbox-autopilot dedupe --account=<ACCOUNT> --json` |
| "Große Anhänge zeigen" | `mailbox-autopilot attachments --account=<ACCOUNT> --size-gt 10mb --json` |

## Exit codes

| Code | Meaning | What to do |
|------|---------|------------|
| 0 | Success | Continue |
| 2 | Connection error | Show stderr, do not retry blindly |
| 3 | Auth missing | Tell user to run `auth set` in a real terminal |
| 4 | Bad arguments / account resolution (`bad_args`, `no_account_selected`, `unknown_account`, `duplicate_alias`, `duplicate_email`, `bootstrap_failed`) | Show stderr; re-check `config list --json` if needed |
| 4 | `preview_mismatch`: the set changed since the dry-run; nothing was moved | Run the dry-run again, show the new count, ask again |
| 5 | `unsubscribe_failed`: the HTTPS unsubscribe failed; nothing was moved | Say it failed, show `manual_unsubscribe` as the by-hand list and `refused_unsubscribe` as "refused as unsafe, do not open". Do not retry automatically |
| 5 | Partial failure / config error (`partial_failure`, `no_config`, `config_corrupt`, `schema_version_unsupported`) | For `partial_failure`: tell the user `moved_count` moved and `not_moved_count` did not; the audit record names the moved UIDs. Do not retry automatically |

## Audit log

Path: `~/.mailbox-cleanup/audit.log`. Append-only JSONL, one record per `--apply` action. Each record now includes an `account` field identifying which alias performed the action. Treat `account` as **optional** for backward compatibility — v0.1 entries pre-date the multi-account schema and may not have it.

## When the CLI isn't enough — stop and report

Messages are moved, deleted or flagged **only through the CLI**. The CLI is what gives
the user a dry-run, the `--expect-count` binding and an audit record; a script has none
of them, however carefully it is written.

If the user asks for something no subcommand or filter can express:

1. Say so plainly: which part of the request the CLI cannot do.
2. Offer the closest CLI route, if there is one, and show its dry-run.
3. Name the gap as a missing CLI capability, so it can be built: the repository is
   `neckarshore-skills/mailbox-autopilot`.

Do **not** write Python or IMAP code that changes messages. That includes `imap_tools`
calls that move, delete, flag or expunge, and raw `MOVE` / `STORE` / `EXPUNGE` commands.

Read-only ad-hoc work is allowed when the CLI has no read command for it, for example
counting messages across all folders, or grouping a folder by recipient address. Run it
with `uv run python3 -` from the plugin directory (system Python lacks `keyring`), read
only, and never write to the audit log.

### IMAP Search Umlaut trap

IMAP SEARCH is ASCII-only, so `--subject-contains "Verlängerung"` cannot match. Use the
ASCII transliteration (`Verlaengerung`) or the English word from a bilingual subject
(`Renewal`, `Extension`).

## Keep-lists

`--keep` takes a full address or a domain written as `@example.com` (subdomains included).
A name such as "The Code" is refused. Find the sender's address in the dry-run's
`by_sender` block, show it to the user, and pass that address.

## Hard rules

1. **Never call any subcommand with `--apply` without showing a dry-run preview first and getting explicit "ja" / "yes" / "apply" from the user.**
2. **Never invent UID lists or counts.** Always use the JSON returned by the CLI.
3. **Never edit the audit log.** It is append-only forensics.
4. **All destructive operations move to Trash.** v1 has no hard-delete; if the user asks "wirklich löschen", explain that v1 only soft-deletes and Trash is purged by the mail provider's retention.
5. **Never mix accounts in a single dry-run/apply pair.** If the user switches account mid-session, re-run the preview against the new account before any `--apply`.
6. **Never change messages outside the CLI.** No script moves, deletes or flags mail. If the CLI cannot do it, stop and report the gap (see "When the CLI isn't enough").
7. **Every `delete --apply` and `move --apply` carries `--expect-count`** with the `affected_count` the user confirmed. No other subcommand accepts it yet; do not pass it there.
