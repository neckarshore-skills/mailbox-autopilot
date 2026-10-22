# Multi-Account Support — Design (v0.2)

**Status:** Approved (2026-05-04), pending implementation plan.
**Predecessor:** `2026-05-04-design.md` (v0.1, single-account).
**Tracking issue:** v2-Backlog item 1 in `omnopsis-planning` brainstorm context.

## 1. Goal

Allow the `mailbox-cleanup` CLI to manage multiple IMAP accounts (initially several IONOS mailboxes; later one Gmail account). Replace the current single-account `--email=...` flow with a config-driven multi-account flow that is non-breaking for existing users via auto-migration.

## 2. Non-Goals (v0.2)

- Gmail OAuth — deferred to v0.3 with full provider abstraction. The `provider` field is reserved as a hook.
- Per-account operational defaults (`default_folder`, `archive_root`) — additive, not needed until the first IONOS/Gmail folder-naming conflict.
- Encrypting the config file — it contains no secrets, only identity + connection metadata.
- TUI / Web UI for config management.

## 3. Architecture

| Layer | Lives in | Contains |
|-------|----------|----------|
| Secrets | macOS Keychain | `mailbox-cleanup:<email> → password` only |
| Identity + connection | `~/.mailbox-cleanup/config.json` | accounts list, default alias, schema version |
| State | (none) | No "last-used" file. Default is explicit in config. |
| Audit | `~/.mailbox-cleanup/audit.log` | Global JSONL, gains an `account` field per entry |

The current Keychain key `imap-server:<email>` is removed during migration — server moves into the config file where it is human-editable.

## 4. Config Schema (v1)

```json
{
  "schema_version": 1,
  "default": "work",
  "accounts": [
    {
      "alias": "work",
      "email": "owner@example.com",
      "server": "imap.ionos.de",
      "port": 993,
      "provider": "ionos"
    },
    {
      "alias": "private",
      "email": "german.privat@example.com",
      "server": "imap.ionos.de",
      "port": 993,
      "provider": "ionos"
    }
  ]
}
```

### Field constraints

| Field | Type | Constraint |
|-------|------|------------|
| `schema_version` | int | Currently `1`. Bumped on breaking changes. Future versions register migration functions. |
| `default` | string | Must match an existing `alias`. Auto-set when only one account exists. May be `null` only if `accounts` is empty. |
| `accounts[].alias` | string | Unique across accounts. Regex `^[a-z0-9][a-z0-9_-]*$`, length 1–32. |
| `accounts[].email` | string | Valid email (basic check: contains `@`, no whitespace). Unique across accounts. |
| `accounts[].server` | string | IMAP server hostname. |
| `accounts[].port` | int | Default `993`. |
| `accounts[].provider` | string | Free-form (see derivation table). Stored as varchar to allow new providers without code change. |

### Provider derivation (when `--provider` is omitted on `auth set`)

| Server hostname matches | provider |
|-------------------------|----------|
| `*.ionos.*` | `ionos` |
| `*.gmail.com`, `*.googlemail.com` | `gmail` |
| `*.mail.me.com`, `*.icloud.com` | `icloud` |
| anything else | `generic` |

File mode `0600` (user-only readable). Created with parent dir `~/.mailbox-cleanup/` mode `0700` if missing.

## 5. CLI Surface

### 5.1 New `config` group

```
mailbox-cleanup config init                      # idempotent; rarely needed (auto-migration covers most cases)
mailbox-cleanup config list [--json]             # tabular by default
mailbox-cleanup config show [<alias>] [--json]   # one account, defaults to current default
mailbox-cleanup config set-default <alias>
mailbox-cleanup config rename <old-alias> <new-alias>
mailbox-cleanup config remove <alias>            # removes from config AND deletes Keychain password
```

### 5.2 Extended `auth` group

```
mailbox-cleanup auth set    --alias=<alias> --email=<email> [--server=...] [--port=993] [--provider=...] [--make-default]
mailbox-cleanup auth test   --account=<alias-or-email> [--json]
mailbox-cleanup auth delete --account=<alias-or-email>
```

`auth set` writes the password to Keychain AND adds the account to the config in a single atomic step. If `--alias` already exists, fails with `duplicate_alias`. `--server` defaults are preserved per provider (IONOS: `imap.ionos.de`).

### 5.3 All other subcommands

`scan`, `senders`, `delete`, `move`, `archive`, `dedupe`, `attachments`, `unsubscribe`, `bounces`:

- Replace `--email <email>` with `--account <alias-or-email>` (optional).
- `--email` retained as a deprecated alias for `--account` for v0.2.x with a stderr deprecation warning. Removed in v0.3.

### 5.4 Account resolution order

For any subcommand that needs an account, the CLI resolves in this priority (highest first):

1. `--account=` flag value
2. `MAILBOX_CLEANUP_ACCOUNT=` env var
3. `default` field in config
4. The single account, if `len(accounts) == 1`
5. Hard-fail with `error_code: "no_account_selected"`

The resolved value may be either an alias or an email; both are looked up against `accounts[].alias` and `accounts[].email`. The two namespaces cannot collide: aliases match `^[a-z0-9][a-z0-9_-]*$` (no `@`), emails always contain `@`.

## 6. Migration

### 6.1 Auto-bootstrap on first invocation

Python's `keyring` library cannot portably enumerate stored entries, so we cannot blindly scan the Keychain for v0.1 accounts. Instead we use the user's first v0.2 invocation as the trigger:

If `~/.mailbox-cleanup/config.json` does **not** exist AND the user supplies `--email=<email>` (their v0.1 habit) AND the Keychain has a password for that email, then before the subcommand executes:

1. Read `imap-server:<email>` from Keychain (fallback `imap.ionos.de`); derive `provider` from server hostname.
2. Derive `alias` from the email local-part, slugified to match `^[a-z0-9][a-z0-9_-]*$` (e.g. `owner@example.com → german`, `first.last@x → first-last`).
3. Write `config.json` with `schema_version: 1`, the single account, and `default = <alias>`.
4. Delete the obsolete `imap-server:<email>` Keychain entry (the password entry is unchanged).
5. Print to stderr: `Migrated to multi-account config (alias: <alias>). Use 'mailbox-cleanup config rename' to change.`
6. Continue executing the original subcommand transparently, treating `--email` as `--account` for this run.

Idempotent: if the config already exists, this routine is skipped (the deprecated-`--email` shim still applies).

### 6.2 Explicit migration / fresh install

For users who don't pass `--email` on first run, or for fresh installs:

```
mailbox-cleanup config init                          # empty config
mailbox-cleanup config init --import-email=<email>   # bootstrap one v0.1 account from Keychain
```

`config init` with no flags creates:

```json
{ "schema_version": 1, "default": null, "accounts": [] }
```

`config init --import-email=<email>` performs steps 1–5 from §6.1 for the supplied email.

When a v0.2 subcommand runs without a config, without `--email`, and without `--account`, the CLI hard-fails with `error_code: "no_account_selected"` and a message pointing at `config init` and `auth set`.

### 6.3 `--email` deprecation

`--email <email>` continues to work in v0.2.x for all subcommands as a synonym for `--account <email>`, emitting:

```
DeprecationWarning: --email is deprecated; use --account=<alias-or-email>. Removed in v0.3.
```

(stderr, not in JSON output). Removed entirely in v0.3.

## 7. Error Handling

| Error code | Meaning | Exit | Where raised |
|------------|---------|------|--------------|
| `no_account_selected` | Multiple accounts exist, no default set, no `--account` flag and no env var | 4 | Account resolver |
| `unknown_account` | `--account=foo` matches no alias or email | 4 | Account resolver |
| `duplicate_alias` | `auth set --alias=X` but X already exists | 4 | `auth set` |
| `duplicate_email` | `auth set --email=X` but X already exists | 4 | `auth set` |
| `config_corrupt` | JSON parse failure | 5 | Config loader |
| `schema_version_unsupported` | Config `schema_version` newer than CLI knows | 5 | Config loader |
| `auth_missing` | (unchanged from v0.1) Keychain entry missing for resolved account | 3 | Auth layer |

All errors emit the existing JSON shape `{"ok": false, "error_code": "...", "message": "..."}` when `--json` is set; otherwise human-readable to stderr. Exit codes match the existing scheme (2 = connection/operation, 3 = auth, 4 = bad args, 5 = config).

## 8. Audit Log Changes

Each JSONL entry gains an `account` field (the resolved alias). Example:

```json
{"timestamp": "2026-05-04T12:34:56Z", "subcommand": "delete", "account": "work", "folder": "INBOX", "args": {}, "affected_uids": [], "result": "success"}
```

`log_action()` gains an `account: str` keyword-only parameter. Existing v0.1 entries without the `account` field remain valid (consumers must treat the field as optional).

## 9. Testing Strategy

### 9.1 Unit tests (`tests/test_config.py`)

- Schema validation: valid config, missing fields, bad alias regex, duplicate aliases, unknown `default`.
- Resolution order: 5 cases (flag, env, default, single-account, hard-fail).
- Auto-migration from mocked Keychain: single account, with/without `imap-server:` key, idempotency on second run, alias collision handling.
- Provider derivation from server hostname: `imap.ionos.de → ionos`, `imap.gmail.com → gmail`, `imap.mail.me.com → icloud`, anything else → `generic`.

### 9.2 CLI tests (`tests/test_cli_multi_account.py`, `click.testing.CliRunner`)

- `config` subcommands: `init`, `list`, `show`, `set-default`, `rename`, `remove` (happy path + error path each).
- `auth set --make-default` writes config + Keychain.
- `auth delete --account=work` removes from both.
- `--email` deprecation warning fires once, output unchanged.
- All subcommands: `--account=alias`, `--account=email`, env-var override, `unknown_account` error path.

### 9.3 Greenmail integration tests (`tests/integration/test_multi_account.py`)

- Spin up Greenmail with two users.
- `auth set` for both, `config set-default work`.
- `scan` without `--account` hits work; `scan --account=private` hits private; `MAILBOX_CLEANUP_ACCOUNT=private scan` hits private even with work as default.
- `auth delete --account=work` then `scan` falls back to single remaining account.

### 9.4 Audit log

- Verify every `--apply` operation writes `account` field in JSONL.
- Verify migration of existing v0.1 audit entries (none required — backward-compatible read).

## 10. Module Layout

New module:

```
src/mailbox_cleanup/config.py         # load, save, validate, resolve, migrate
tests/test_config.py
tests/test_cli_multi_account.py
tests/integration/test_multi_account.py
```

Modified:

```
src/mailbox_cleanup/auth.py           # set_credentials gains alias param; delete_credentials by-alias
src/mailbox_cleanup/cli.py            # new config group; --account everywhere; --email deprecated shim
src/mailbox_cleanup/audit.py          # account field in log_action signature
README.md                             # new auth + config workflow
skill/SKILL.md                        # mirror update
```

Public API of `auth.py` keeps `Credentials` dataclass unchanged so all `imap_connect(creds)` callsites continue to work.

## 11. Out-of-Scope (Explicit)

- Gmail OAuth — v0.3 will introduce a `Provider` abstraction. The `provider` field exists today as a forward-compatible hook.
- Per-account `default_folder` / `archive_root` — additive, schema-compatible. Add when first IONOS/Gmail conflict appears.
- Multi-machine config sync — config is local. Users who want sync can symlink the file.
- Encryption — config holds no secrets. Email addresses are PII but not security-critical at this scope.

## 12. Schema Compatibility Promise

- v0.2.x will only ever read `schema_version: 1`.
- v0.3+ that introduces additive fields (e.g. per-account defaults) MUST keep `schema_version: 1` as long as the file is forward-compatible (additive fields ignored by v0.2).
- Breaking changes bump `schema_version` and ship a migration in `config.py`.
