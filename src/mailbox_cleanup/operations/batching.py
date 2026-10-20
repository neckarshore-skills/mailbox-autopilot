"""Move UIDs in batches. A single MOVE with thousands of UIDs times out on IONOS.

A move removes only the UIDs it was given (#73):

- Server has MOVE: native UID MOVE.
- No MOVE, but UIDPLUS: UID COPY, then STORE \\Deleted, then UID EXPUNGE scoped to the
  same UIDs. Never a plain EXPUNGE, which would also remove every message another
  client had only marked \\Deleted.
- Neither: refused before anything is touched (MoveUnsupportedError).

The MOVE command is sent here, not through imap_tools' MailBox.move(): that method
decides a second time, on the raw case-sensitive capability list, and its fallback is
the unscoped EXPUNGE (James on #79). One decision, made here.
"""

from collections.abc import Sequence

from imap_tools.utils import encode_folder

BATCH_SIZE = 500


class PartialMoveError(RuntimeError):
    """Some batches moved, then one failed. `moved` is what actually left the folder."""

    def __init__(self, moved: list[str], remaining: list[str], cause: Exception):
        self.moved = moved
        self.remaining = remaining
        self.cause = cause
        super().__init__(
            f"moved {len(moved)} of {len(moved) + len(remaining)} messages, then failed: "
            f"{type(cause).__name__}"
        )


class MoveUnsupportedError(RuntimeError):
    """The server offers neither MOVE nor UIDPLUS, so no move can be scoped to our UIDs."""


def capabilities(mb) -> frozenset[str]:
    out = set()
    for c in mb.client.capabilities:
        out.add((c.decode() if isinstance(c, bytes) else str(c)).upper())
    return frozenset(out)


def _check(result, what: str) -> None:
    if not result or result[0] != "OK":
        raise RuntimeError(f"{what} failed: {result!r}")


def ensure_scoped_move(mb) -> frozenset[str]:
    """Raise MoveUnsupportedError unless a move can be scoped to our UIDs.

    Callers with a side effect outside the mailbox (unsubscribe) call this first.
    """
    caps = capabilities(mb)
    if "MOVE" not in caps and "UIDPLUS" not in caps:
        raise MoveUnsupportedError(
            "The server supports neither MOVE nor UIDPLUS; a move could remove messages "
            "outside the selection, so nothing was moved."
        )
    return caps


def _move_batch(mb, batch: list[str], target: str, caps: frozenset[str]) -> None:
    uidset = ",".join(batch)
    if "MOVE" in caps:
        _check(mb.client.uid("MOVE", uidset, encode_folder(target)), "UID MOVE")
        return
    _check(mb.client.uid("COPY", uidset, encode_folder(target)), "UID COPY")
    _check(mb.client.uid("STORE", uidset, "+FLAGS.SILENT", r"(\Deleted)"), "STORE \\Deleted")
    _check(mb.client.uid("EXPUNGE", uidset), "UID EXPUNGE")


def move_in_batches(mb, uids: Sequence[str], target: str, size: int = BATCH_SIZE) -> list[str]:
    """Move `uids` to `target`, `size` at a time. Raises PartialMoveError on a failed batch."""
    uids = list(uids)
    if not uids:
        return []
    caps = ensure_scoped_move(mb)
    moved: list[str] = []
    for i in range(0, len(uids), size):
        batch = uids[i : i + size]
        try:
            _move_batch(mb, batch, target, caps)
        except Exception as e:
            raise PartialMoveError(moved, uids[i:], e) from e
        moved.extend(batch)
    return moved
