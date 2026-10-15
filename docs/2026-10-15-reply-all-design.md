# `manage draft`: reply to everyone by default

Status: built in 0.6.0. Quoting the answered mail below the reply is a second, separate change.

## Decisions

| # | Decision | Who, when |
|---|---|---|
| 1 | A reply goes to everyone on the answered mail by default | Founder, 2026-10-15 |
| 2 | The user may leave individual recipients out before the draft is written | Founder, 2026-10-15 |
| 3 | More than 10 recipients is refused with a clear message; `--sender-only` is offered | Founder, 2026-10-15 |

## The constraint

The recipients of a reply come from the answered mail, and its sender controls every address in it. A free `--to` or `--cc` on `manage draft` would add a second, unguarded way to address a draft (see the compose design, `docs/2026-10-08-compose-design.md`, and the security review recorded there). So the program computes the recipients, and the caller can only take addresses away:

1. `--drop <address>` removes one planned recipient. An address that is not planned is refused (`not_a_recipient`), so `--drop` cannot be used to probe for or add anyone.
2. `--sender-only` narrows to the sender.
3. `--preview` returns the planned `to`, `cc` and `subject` and writes nothing. The skill shows them and waits for the user's yes. That gate is the skill's, not a mechanism.

## The rules

1. **Main recipient:** Reply-To, else From.
2. **Reply-all:** the original To join To, the original Cc stay in Cc, the way mail clients do it.
3. **Own address** removed, compared without case. Aliases are a known gap: the configuration holds one address per account.
4. **Duplicates** across From, To and Cc appear once.
5. **The owner's own mail:** when the main recipient is the owner, the reply goes to that mail's To instead of back to the owner. This also holds with `--sender-only`.
6. **Unusable addresses** (anything that is not one bare ASCII address after parsing) are left out with a warning that gives their count, never the value. An unusable sender keeps its own warning.
7. **Dropping every To recipient** moves the first Cc up into To.
8. **More than 10** after drops: refused with `too_many_recipients`, never truncated. Same limit as `manage compose`.
9. **Writing:** To and Cc are set as address objects and read back (`headers.set_addresses`), the same two locks `manage compose` uses. The command's output reports `to` and `cc` read back from the built draft.

## Output change

`to` in the output of `manage draft` was a string and is now a list, next to a new `cc` list. The skill in the same release reads the lists.
