"""Hold the probe's report against the run sheet's "Expected" column, arm by arm.

Runs on Obi's laptop over the report of step 16, not on the target Mac: it reads uids,
outcome classes and OSStatus codes, never a value. Sitting 1 said `none` for arm 2 where
the sheet expected locked, and only a person reading the log noticed. This names it.

Usage: python3 check.py <report.jsonl>
One line per expectation: MATCHES, DEVIATES or OPEN (no record yet, the sitting is partial).
Exit 0 only when every arm matches. 1: an arm deviates. 2: no report, or a line that is not
JSON. 3: nothing deviates but an arm is open, so the report is not the whole sitting.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from show import reached  # noqa: E402  one judgement of "the read could reach it", not two

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


# The path route keeps the Security framework's own status (job.read_at).
PATH_CLASS = {-25300: MISSING, -25308: LOCKED}


def _list_class(read: dict) -> str:
    if read.get("value_equals_canary"):
        return CANARY
    if read.get("outcome") == "KeyringLocked":
        return LOCKED
    if read.get("outcome") == "none":
        return MISSING
    return NO_CANARY


def _path_class(read: dict) -> str:
    if read.get("value_equals_canary"):
        return CANARY
    return PATH_CLASS.get(read.get("osstatus"), NO_CANARY)


def _routes(r: dict) -> tuple[list[tuple[str, str | None, str]], str]:
    """(route, class or None, detail) per route the record holds, or a reason it holds none.
    A list route the job set but did not reach is a route with no class: in sitting 2 its
    `none` was taken for "missing"."""
    if "failed" in r:
        return [], f"failed in the job ({r['failed']})"
    out = []
    search = r.get("search")
    if "read" in r and (search is None or reached(r)):
        read = r["read"]
        detail = f"list {read.get('outcome')}, OSStatus {read.get('osstatus')}"
        out.append(("list", _list_class(read), detail))
    elif search is not None:
        out.append(("list", None, f"not on the effective search list ({search.get('domain')})"))
    if "read_path" in r:
        read = r["read_path"]
        out.append(("path", _path_class(read), f"path OSStatus {read.get('osstatus')}"))
    return out, "" if out else "no read in the record"


def _fits(want: str, routes: list[tuple[str, str | None, str]]) -> bool:
    """A canary is expected on at least one route; everything else on every route."""
    if not routes or any(got is None for _, got, _ in routes):
        return False
    if want == CANARY:
        return any(got == CANARY for _, got, _ in routes)
    if want == NO_CANARY:
        return all(got != CANARY for _, got, _ in routes)
    return all(got == want for _, got, _ in routes)


def judge(records: list[dict]) -> list[tuple[str, str, str]]:
    """(verdict, arm/mode, text) per expectation. A re-run arm is judged by its last line."""
    last = {_key(r): r for r in records}
    out = []
    for key, (want, console) in EXPECTED.items():
        r = last.get(key)
        if r is None:
            out.append(("OPEN", key, f"expected {want}, no record yet"))
            continue
        routes, why = _routes(r)
        ok = _fits(want, routes)
        detail = why or "; ".join(d for _, _, d in routes)
        got = ", ".join(f"{name} {cls or '-'}" for name, cls, _ in routes) or "-"
        who = (r.get("facts") or {}).get("console_user")
        if ok and console is not None and who != console:
            ok, detail = False, f"{detail}, console {who}, expected {console}"
        verdict = "MATCHES" if ok else "DEVIATES"
        out.append((verdict, key, f"expected {want}, got {got}: {detail}"))
    halves = [last.get(k) for k in ARM3]
    if all(h is None for h in halves):
        out.append(("OPEN", "arm3", "expected a canary in one half, no record yet"))
    else:
        read = [
            f"{k} ({name})"
            for k, h in zip(ARM3, halves, strict=True)
            if h is not None
            for name, cls, _ in _routes(h)[0]
            if cls == CANARY
        ]
        if read:
            out.append(("MATCHES", "arm3", f"canary read by {', '.join(read)}"))
        else:
            out.append(("DEVIATES", "arm3", "expected a canary in one half, none read"))
    return out


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: python3 check.py <report.jsonl>", file=sys.stderr)
        return 2
    try:
        with open(argv[1], encoding="utf-8") as f:
            records = [json.loads(line) for line in f if line.strip()]
    except (OSError, json.JSONDecodeError) as e:
        print(f"cannot read the report {argv[1]}: {e}", file=sys.stderr)
        return 2
    results = judge(records)
    for verdict, key, text in results:
        print(f"{verdict} {key}: {text}")
    verdicts = {v for v, _, _ in results}
    if "DEVIATES" in verdicts:
        return 1
    return 3 if "OPEN" in verdicts else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
