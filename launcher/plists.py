"""The two LaunchDaemons of the bot (#76, L3, L9, L13, L5), written by code so a test can
read them back with plistlib. install.sh runs this as root and installs the output.

Both are system daemons with no UserName key: the launcher starts as root, so the
credential step (L11) can run as root and the session drops to the bot user in gate.py.
"""

from __future__ import annotations

import plistlib
import sys

BASE = "/Library/Application Support/mailbox-autopilot"
LABEL = "ai.neckarshore.mailbox-autopilot"
RETENTION_LABEL = f"{LABEL}.retention"
UV = "/usr/local/mailbox-autopilot/uv/uv"
UV_HOME = "/usr/local/mailbox-autopilot/uv"

FIRST_HOUR, LAST_HOUR = 7, 20  # L9 (D10): hourly from 07:00 to 20:00
RETENTION_AT = {"Hour": 5, "Minute": 30}  # outside the run hours and the night backup window
EXIT_TIMEOUT_S = 20  # SIGTERM to SIGKILL; gate.py kills the session on SIGTERM


def launcher() -> dict:
    return {
        "Label": LABEL,
        "ProgramArguments": ["/bin/bash", f"{BASE}/launcher/launch.sh"],
        "StartCalendarInterval": [
            {"Hour": h, "Minute": 0} for h in range(FIRST_HOUR, LAST_HOUR + 1)
        ],
        "RunAtLoad": False,
        "StandardOutPath": "/dev/null",
        "StandardErrorPath": f"{BASE}/launcher.stderr",  # L13
        "ExitTimeOut": EXIT_TIMEOUT_S,
        "ProcessType": "Background",
    }


def retention() -> dict:
    return {
        "Label": RETENTION_LABEL,
        "ProgramArguments": [
            "/usr/bin/env",
            "-i",
            "PATH=/usr/bin:/bin",
            "HOME=/var/empty",
            f"UV_PYTHON_INSTALL_DIR={UV_HOME}/python",
            f"UV_CACHE_DIR={UV_HOME}/cache",
            "UV_PYTHON_DOWNLOADS=never",
            UV,
            "run",
            "--no-config",
            "--no-project",
            "--offline",
            "--python",
            "3.11",
            "--python-preference",
            "only-managed",
            f"{BASE}/launcher/retention.py",
        ],
        "StartCalendarInterval": RETENTION_AT,
        "RunAtLoad": False,
        "StandardOutPath": "/dev/null",
        "StandardErrorPath": f"{BASE}/retention.stderr",
        "ProcessType": "Background",
    }


def main(argv: list[str]) -> int:
    which = {"launcher": launcher, "retention": retention}
    if len(argv) != 2 or argv[1] not in which:
        print("usage: plists.py launcher|retention", file=sys.stderr)
        return 2
    sys.stdout.buffer.write(plistlib.dumps(which[argv[1]]()))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
