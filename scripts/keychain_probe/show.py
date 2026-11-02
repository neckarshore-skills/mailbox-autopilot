"""Reading job.py's JSON: one line per arm for the person at the keyboard, and the two
answers the runner's stop rules need."""

import json
import sys

HALVES = (("arm3a", "none"), ("arm3b", "unsigned"), ("arm3c", "cdhash"))


def line(r: dict) -> str:
    head = f"{r.get('arm')}/{r.get('mode')}"
    if "failed" in r:
        return f"{head}: FAILED in the job, {r['failed']}"
    facts = r.get("facts", {})
    who = f"uid {facts.get('uid')}, console {facts.get('console_user')}"
    read = r.get("read")
    if read is None:
        step = r.get("prepare", r.get("reset", {}))
        return f"{head}: {r.get('action')} {json.dumps(step)} ({who})"
    canary = "CANARY READ" if read.get("value_equals_canary") else "no canary"
    return f"{head}: {canary}, {read.get('outcome')}, OSStatus {read.get('osstatus')} ({who})"


def records(path: str) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        return [json.loads(text) for text in f if text.strip()]


def prepared(path: str) -> bool:
    """Every `prepare` in this file worked. A failed one makes the next arm meaningless."""
    found = records(path)
    return bool(found) and all(r.get("prepare", {}).get("rc") == 0 for r in found)


def winner(path: str) -> str:
    """The first half of arm 3 that read the canary, or nothing."""
    read = {r.get("arm"): r.get("read", {}) for r in records(path)}
    for arm, partition in HALVES:
        if read.get(arm, {}).get("value_equals_canary"):
            return partition
    return ""


if __name__ == "__main__":
    if sys.argv[1] == "--prepared":
        sys.exit(0 if prepared(sys.argv[2]) else 1)
    if sys.argv[1] == "--winner":
        print(winner(sys.argv[2]))
        sys.exit(0)
    for record in records(sys.argv[1]):
        print(line(record))
