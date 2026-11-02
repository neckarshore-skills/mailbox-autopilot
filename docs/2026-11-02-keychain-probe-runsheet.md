# Keychain probe: run sheet

**For the owner, at the target Mac, with Obi on the line.** It answers #76 L11: can a launchd job of a user who never logs in read a keychain? Without an answer the launcher cannot start. The design is James's read of 2026-11-02, section 6.

> **Important:** This creates a throwaway user and a LaunchDaemon, and it reboots the target Mac twice. Do not reboot around 02:30, when the Paperless backup runs. Plan about 45 minutes.

## What it touches

| # | Thing | Removed by `cleanup` |
|---|-------|----------------------|
| 1 | User `mbxprobe`, standard, not an admin | ✅ |
| 2 | Group `mbxprobe-bot` | ✅ |
| 3 | `/Library/mailbox-autopilot-probe`: uv 0.13.5 by checksum, Python 3.11 and 3.12, the locked keyring | ✅ |
| 4 | `/Library/LaunchDaemons/ai.neckarshore.keychain-probe.plist`, one job at a time | ✅ |
| 5 | A keychain item with the value `CANARY` | ✅ with the user |

It uses no real password, no Claude token (arm 8 is not released) and no mailbox. The only file it leaves is `/Users/Shared/keychain-probe-report.jsonl`, which holds uids, paths and result codes. Keep `auth test` output out of it and out of the PR (#82).

**Measured on the target Mac, 2026-11-02:** macOS 27.0.1 on arm64. FileVault is off, so a reboot comes back without a password. Auto-login is on for `german`. uv and this repo are not installed there, which is why the script brings uv itself.

## Before you start

On your laptop, Obi copies the script over:

```bash
scp -r scripts/keychain_probe m1:keychain-probe
```

Then, in one terminal on the target Mac (at the machine or `ssh -t m1`):

```bash
cd ~/keychain-probe
sudo ./probe.sh status
```

Expected: `no user, group, directory or daemon of the probe is left`.

## The sitting

Run the steps top to bottom. Each prints one line per arm; paste those lines to Obi.

| # | Step | Command | Expected |
|---|------|---------|----------|
| 1 | Set up, about 3 minutes | `sudo ./probe.sh setup` | Ends with `done. Next: arm1` |
| 2 | Arm 1: never logged in | `sudo ./probe.sh arm1` | `no canary`: no keychain at all |
| 3 | Arm 2: a keychain, locked | `sudo ./probe.sh arm2` | `no canary`, OSStatus -25308 or similar |
| 4 | Arm 3: the job unlocks it | `sudo ./probe.sh arm3` | `CANARY READ` in `arm3a` or `arm3b` |
| 5 | Arm 6: no trust entry, other interpreter | `sudo ./probe.sh arm6` | `no canary` twice |
| 6 | Arm 7: red control, wrong service | `sudo ./probe.sh arm7` | `no canary`, outcome `none` (-25300 is mapped to it) |
| 7 | Arm 4, before the reboot. Add `--partition` if only `arm3b` read the canary | `sudo ./probe.sh arm4-install` | `installed for the next boot` |
| 8 | Reboot | `sudo shutdown -r now` | Back in about 2 minutes |
| 9 | Arm 4: at boot | `sudo ./probe.sh arm4-read` | `CANARY READ`, `facts.boottime` is the new boot |
| 10 | Arm 5, before the GUI login | `sudo ./probe.sh arm5-reset` | Prints the password and one command |
| 11 | One GUI login as **Keychain Probe**, at the machine or by Screen Sharing. `german` is logged in by auto-login, so use Fast User Switching in Control Center, which keeps his session running. If it is off, log `german` out; step 12 brings him back, and what runs in his session stops until then. Skip every assistant screen, run the printed command in that user's Terminal, and log out | By hand | Terminal returns without an error |
| 12 | Reboot | `sudo shutdown -r now` | Back in about 2 minutes |
| 13 | Arm 5: after one login | `sudo ./probe.sh arm5-read` | `no canary`, locked |
| 14 | Keep the evidence | `sudo ./probe.sh report` | One JSON line per job |
| 15 | Clean up, then remove the copy | `sudo ./probe.sh cleanup && cd ~ && rm -r keychain-probe` | `no user, group, directory or daemon of the probe is left` |

After each reboot, check that the services of this machine came back, as you would after any restart.

## What the result decides

| # | Result | Decision |
|---|--------|----------|
| 1 | Arms 3 and 4 read the canary | The fallback of L11 works: a dedicated keychain, unlocked by the launcher's root part. L11 gets this route, and the launcher can be specified |
| 2 | Arm 3 reads it and arm 4 does not | Something in the boot state is missing. Obi reads the arm 4 facts before anything else |
| 3 | Neither reads it | No route exists yet. MASCHIN ruled out a credential file, so this goes back to him and James |
| 4 | Arm 7 reads the canary, or says anything but missing | The probe cannot tell missing from locked, so no arm counts. Stop and tell Obi |

**Two things this cannot show.** "Nobody logged in" in James's table is not this machine's state: `german` logs in by himself after each reboot, and the sheet leaves that as it is, because that is how the target Mac runs. The partition list in `arm3b` is Obi's addition, not James's, because `add-generic-password` writes only Apple's partitions; `facts.codesign` shows what signs the interpreter.

If a step stops with an error, do not retry it. `sudo ./probe.sh cleanup` removes what the steps created. Its report and your copy of the script stay until you remove them.
