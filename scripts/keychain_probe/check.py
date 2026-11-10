"""Hold the probe's report against the run sheet's "Expected" column, arm by arm.

Runs on Obi's laptop over the report of step 16, not on the target Mac: it reads uids,
outcome classes and OSStatus codes, never a value. Sitting 1 said `none` for arm 2 where
the sheet expected locked, and only a person reading the log noticed. This names it.

Usage: python3 check.py <report.jsonl>   exit 1 if any arm deviates, else 0.
One line per expectation: MATCHES, DEVIATES or OPEN (no record yet, the sitting is partial).
"""

import json
import sys

CANARY, NO_CANARY, LOCKED, MISSING = "canary", "no canary", "locked", "missing"

# arm/mode -> (expected class, the owner logged in as console user, or None for any).
# The source is the run sheet's table, docs/2026-11-02-keychain-probe-runsheet.md.
EXPECTED = {
    "arm1/drop": (NO_CANARY, None),
    "arm1/username": (NO_CANARY, None),
    "arm2/drop": (NO_CANARY, None),  # which outcome a locked item gives is unmeasured
    "arm2/username": (NO_CANARY, None),
    "arm4/drop": (CANARY, None),  # the console user shows who won the race; any is fine
    "arm4b/drop": (CANARY, "german"),
    "arm5/drop": (LOCKED, None),
    "arm5/username": (LOCKED, None),
    "arm6a/drop": (NO_CANARY, None),
    "arm6b/drop": (NO_CANARY, None),
    "arm7/drop": (MISSING, None),
}
ARM3 = ("arm3a/drop", "arm3b/drop", "arm3c/drop")  # at least one half reads the canary


def _key(r: dict) -> str:
    return f"{r.get('arm')}/{r.get('mode')}"


def _got(r: dict) -> tuple[str | None, str]:
    """The class a record shows, or None when it holds no read, with the reason."""
    if "failed" in r:
        return None, f"failed in the job ({r['failed']})"
    search = r.get("search")
    if search is not None and not search.get("on_list"):
        return None, f"not on the search list ({search.get('domain')}), no read"
    read = r.get("read")
    if read is None:
        return None, "no read in the record"
    detail = f"outcome {read.get('outcome')}, OSStatus {read.get('osstatus')}"
    if read.get("value_equals_canary"):
        return CANARY, detail
    if read.get("outcome") == "KeyringLocked":
        return LOCKED, detail
    if read.get("outcome") == "none":
        return MISSING, detail
    return NO_CANARY, detail


def _fits(want: str, got: str | None) -> bool:
    if got is None:
        return False
    if want == NO_CANARY:
        return got != CANARY
    return want == got


def judge(records: list[dict]) -> list[tuple[str, str, str]]:
    """(verdict, arm/mode, text) per expectation. A re-run arm is judged by its last line."""
    last = {_key(r): r for r in records}
    out = []
    for key, (want, console) in EXPECTED.items():
        r = last.get(key)
        if r is None:
            out.append(("OPEN", key, f"expected {want}, no record yet"))
            continue
        got, detail = _got(r)
        ok = _fits(want, got)
        who = (r.get("facts") or {}).get("console_user")
        if ok and console is not None and who != console:
            ok, detail = False, f"{detail}, console {who}, expected {console}"
        verdict = "MATCHES" if ok else "DEVIATES"
        out.append((verdict, key, f"expected {want}, got {got or '-'}: {detail}"))
    halves = [last.get(k) for k in ARM3]
    if all(h is None for h in halves):
        out.append(("OPEN", "arm3", "expected a canary in one half, no record yet"))
    else:
        read = [
            k for k, h in zip(ARM3, halves, strict=True) if h is not None and _got(h)[0] == CANARY
        ]
        if read:
            out.append(("MATCHES", "arm3", f"canary read by {', '.join(read)}"))
        else:
            out.append(("DEVIATES", "arm3", "expected a canary in one half, none read"))
    return out


def main(argv: list[str]) -> int:
    with open(argv[1], encoding="utf-8") as f:
        records = [json.loads(line) for line in f if line.strip()]
    results = judge(records)
    for verdict, key, text in results:
        print(f"{verdict} {key}: {text}")
    return 1 if any(v == "DEVIATES" for v, _, _ in results) else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
