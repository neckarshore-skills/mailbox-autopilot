"""One line per arm from job.py's JSON, for the person at the keyboard."""

import json
import sys


def line(r: dict) -> str:
    head = f"{r.get('arm')}/{r.get('mode')}"
    if "failed" in r:
        return f"{head}: FAILED in the job, {r['failed']}"
    facts = r.get("facts", {})
    who = f"uid {facts.get('uid')}, manager {facts.get('manager')}"
    read = r.get("read")
    if read is None:
        step = r.get("prepare", r.get("reset", {}))
        return f"{head}: {r.get('action')} {json.dumps(step)} ({who})"
    canary = "CANARY READ" if read.get("value_equals_canary") else "no canary"
    return f"{head}: {canary}, {read.get('outcome')}, OSStatus {read.get('osstatus')} ({who})"


if __name__ == "__main__":
    with open(sys.argv[1], encoding="utf-8") as f:
        for text in f:
            print(line(json.loads(text)))
