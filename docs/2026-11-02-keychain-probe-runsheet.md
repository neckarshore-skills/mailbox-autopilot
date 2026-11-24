# Keychain probe: run sheet

**For the owner, at the target Mac, with Obi on the line.** It answers #76 L11: can a launchd job of a user who never logs in read a keychain? Without an answer the launcher cannot start. The design is James's read of 2026-11-02, section 6, and his read of this probe on #97.

> **Important:** This creates a throwaway user and a LaunchDaemon, and it reboots the target Mac twice. The Mac was last rebooted on 2026-11-21 at 15:15 and the backup ran after it (last success 2026-11-24 01:30 UTC, read over SSH before sitting 2), so step 15 proves again that the backup survives a restart, not for the first time. Do not reboot between 02:00 and 04:00, and be done with step 12 before 02:00. Plan about an hour.

## What it touches

| # | Thing | Removed by `cleanup` |
|---|-------|----------------------|
| 1 | User `mbxprobe`, standard, not an admin | ✅ |
| 2 | Group `mbxprobe-bot` | ✅ |
| 3 | `/Library/mailbox-autopilot-probe`: uv 0.13.5 by checksum, Python 3.11 and 3.12, the locked keyring | ✅ |
| 4 | `/Library/LaunchDaemons/ai.neckarshore.keychain-probe.plist`, one job at a time | ✅ |
| 5 | A keychain item with the value `CANARY` | ✅ with the user |

It uses no real password, no Claude token (arm 8 is not released) and no mailbox. It touches nothing of the backup, its files or the Docker stack.

**What stays after `cleanup`:**

| # | Thing | Effect |
|---|-------|--------|
| 1 | `/Users/Shared/keychain-probe-report.jsonl` | Uids, paths and result codes, no secret. You remove it in step 18 |
| 2 | Your copy in `~/keychain-probe` | You remove it in step 18 |
| 3 | Per-user directories under `/private/var/folders` for the deleted uid | A harmless orphan, unmeasured. A later user with the same uid inherits it |
| 4 | A Background Task Management record for the daemon's label, and a notification to `german` | Harmless, unmeasured. `status` names the record and exits 1 while it is there. In sitting 1 it was gone after `cleanup` |
| 5 | Only if `dynamic` held in arm 2: a dynamic search list for the throwaway user | `cleanup` does not touch it and `status` does not check it. Where it lives, whether a reboot clears it and whether it reaches anything outside the throwaway user are unmeasured |

**Sitting 2, 2026-11-24, stopped after arm 3, again on a probe defect.** Arms 1 to 3 ran; no half of arm 3 read the canary, the stop rule held arms 4, 6 and 7, nothing was rebooted, and `cleanup` left nothing. The records show why: in every reading job `list-keychains -d user` held the probe keychain, while plain `list-keychains`, the effective list a read goes through, held only the System keychain twice. The guard compared the first list, so arm 2's `on the search list (user)` was wrong and every read since went past a keychain it could not reach. L11 is still unmeasured. Since then the job judges the effective list, `show.py` and `check.py` judge it once more from the record, and every reading job also reads **by path** (`read_at`): it names the keychain in the query itself, so it needs no search list at all. The path read keeps the Security framework's own status code instead of turning not-found into `none`.

Keep `auth test` output out of the report, the PR, the ticket and any agent session (#82).

**Sitting 1, 2026-11-03, stopped after arm 3 (#97).** Arm 2 said `none` where the sheet expected locked. Every job's search list held only the System keychain, although the throwaway user's plist on disk named the probe keychain: a list set in one job does not reach the next, and keyring 25.7.0 ignores a keychain path. So no read could reach the canary, and the stop rule of arm 3 fired for the probe's reason, not the Mac's. Since then every reading job puts the keychain on its own search list first and refuses to read if it is not there. What sitting 1 did measure: a job that drops from root to the user can unlock and lock a dedicated keychain by path.

> **Warning:** `probe.sh` has never run against the real thing. Past its `guard` it has only walked through tests with stand-ins for launchd and the system tools, under bash 3.2 and in CI. That shows the script holds together, not that `launchctl`, `sysadminctl` or `security` answer as expected in a job with no session. So step 1 is its first real run. If a step behaves differently from this sheet, stop and tell Obi.

**Measured on the target Mac, 2026-11-02:** macOS 27.0.1 on arm64. FileVault is off, so a reboot comes back without a password. Auto-login is on for `german`, uid 501. uv and this repo are not installed there, which is why the script brings uv itself.

## Before you start

Obi copies the script over from the laptop, from the head James read, so the mode survives, and compares the sha256 sums on both sides (four files; `check.py` runs on the laptop, not here). Sitting 1 and 2 used 90aff49; sitting 3 uses the head James reads after the sitting 2 fix:

```bash
git archive <head> scripts/keychain_probe | ssh m1 'mkdir ~/keychain-probe && tar -xf - --strip-components=2 -C ~/keychain-probe && shasum -a 256 ~/keychain-probe/*'
```

Obi also names the backup's LaunchAgent label and status file. They are in the install record and stay out of this public repo; below they read `<label>` and `<status file>`.

Then, in one terminal on the target Mac (at the machine or `ssh -t m1`):

```bash
cd ~/keychain-probe
sudo ./probe.sh status
```

Expected: `no user, group, directory, daemon or report of the probe is left`.

## The sitting

Run the steps top to bottom. Each prints one line per arm. Paste those lines to Obi, **except the password line of step 11**.

**No `security` write as `german` on this Mac, for diagnosis either.** A write without a keychain lands in his login keychain, and with a path that does not exist it lands there too, without a word. That happened once while this probe was built.

| # | Step | Command | Expected |
|---|------|---------|----------|
| 1 | Set up, about 3 minutes | `sudo ./probe.sh setup` | Ends with `done. Next: arm1` |
| 2 | Arm 1: never logged in | `sudo ./probe.sh arm1` | `no canary`: no keychain at all |
| 3 | Arm 2: a keychain, locked. It first looks for a search list domain that reaches the job's effective list: `user`, else `dynamic`. Sitting 2 says probably neither, and that no longer stops the run | `sudo ./probe.sh arm2` | Either `on the search list (user)` or `(dynamic)`, or two `NOT ON THE SEARCH LIST` lines and `the path route reads alone`; then `search list domain that held:` (a domain or `none`); then two lines with `no canary` on every route. The path route's OSStatus for a locked keychain is unmeasured: tell Obi what it says |
| 4 | Arm 3: the job unlocks it, in three halves, and reads on both routes. **Stop rule below** | `sudo ./probe.sh arm3` | `CANARY READ` on `list:` or `path:` in at least one of `arm3a`, `arm3b`, `arm3c`, then `route and partition that read:`, for example `path cdhash` |
| 5 | Arm 6: no trust entry, other interpreter | `sudo ./probe.sh arm6` | `no canary` twice |
| 6 | Arm 7: red control, wrong service | `sudo ./probe.sh arm7` | `no canary`: on the list route outcome `none`, on the path route OSStatus `-25300` (not found) |
| 7 | Arm 4, before the reboot | `sudo ./probe.sh arm4-install` | `installed for the next boot` |
| 8 | Backup check, then reboot | `launchctl print gui/501/<label> \| grep 'state ='`, then `sudo shutdown -r now` | `not running`. Back in about 2 minutes |
| 9 | Arm 4: the job at boot | `sudo ./probe.sh arm4-read` | `CANARY READ`. The console user shows who won the race against auto-login |
| 10 | Arm 4b: the state of the 14 runs a day. Wait until `german` is logged in | `sudo ./probe.sh arm4b` | `CANARY READ`, console `german` |
| 11 | Arm 5, before the GUI login | `sudo ./probe.sh arm5-reset` | Prints the password and one command. Do not paste the password line |
| 12 | One GUI login as **Keychain Probe**, at the machine or by Screen Sharing, before 02:00. `german` is logged in by auto-login, so use Fast User Switching in Control Center, which keeps his session running. If it is off, log `german` out; step 13 brings him back, and his session, the backup included, stops until then. Skip every assistant screen, run the printed command in **that user's** Terminal, and log out | By hand | Terminal prints nothing. `NOT DONE` means the wrong user or a failed add: stop and tell Obi |
| 13 | Backup check, then reboot | As in step 8 | `not running`. Back in about 2 minutes |
| 14 | Arm 5: after one login, reading the login keychain that login made | `sudo ./probe.sh arm5-read` | `no canary`, locked. `NOT ON THE SEARCH LIST` means the login made no login keychain: tell Obi |
| 15 | Backup proof, as `german`, no `sudo`. It must stay `kickstart`: the backup script started directly in an SSH session fails on the Keychain, which would be a false alarm. Whether `kickstart` works over SSH is unmeasured; if it refuses, do this step at the machine. How long a run takes is unmeasured too; wait for it | `launchctl kickstart gui/501/<label>`, then read `<status file>` | Exit 0, with today's time |
| 16 | Keep the evidence | `sudo ./probe.sh report` | One JSON line per job. Obi takes it and runs `python3 scripts/keychain_probe/check.py <report>` on the laptop: one line per arm, `DEVIATES` wherever the report differs from this table's Expected column. Exit 0 only when every arm matches; 1 an arm deviates, 2 the report is unreadable, 3 an arm has no record (`OPEN`, the sitting is partial) |
| 17 | Clean up | `sudo ./probe.sh cleanup` | Exits 1 and names the report, because it is still there |
| 18 | After Obi has the report | `sudo rm /Users/Shared/keychain-probe-report.jsonl`, then `sudo ./probe.sh status`, then `cd ~ && rm -r keychain-probe` | The closing line of `status`. A remaining background task record is named instead, see "What stays" |
| 19 | In `german`'s own Terminal, no `sudo`. It only looks and prints no value | `security find-generic-password -s mailbox-cleanup-probe`, then `echo $?` | `44`: no canary in his keychain. Anything else: tell Obi, and delete nothing |

**Stop rules, all enforced by the script.** If no domain reaches the effective list in arm 2, the list route is out and every later job reads by path only; that is not a stop. If a domain did reach it, every later reading job sets it and must reach the keychain through the effective list, or the run stops there instead of reporting `no canary`. If no half of arm 3 reads the canary on either route, arms 4, 6 and 7 refuse to run, because they mean something only with the route and partition arm 3 proved. A `prepare` that fails stops the run at once. In every case do not reboot. Tell Obi, then run `cleanup`.

If any other step stops with an error, do not retry it. `sudo ./probe.sh cleanup` removes what the steps created, also after a failed user deletion, and an interrupted sitting does no harm: arm 4's job would run once at the next boot and read a canary. **If you stop during step 12 and logged `german` out, log him back in before 02:00,** or the night's backup does not run.

## What the result decides

| # | Result | Decision |
|---|--------|----------|
| 1 | Arms 3, 4 and 4b read the canary on the **list** route | The fallback of L11 works: a dedicated keychain, unlocked by the launcher's root part, read by keyring as it is. L11 gets this route, and the launcher can be specified. The partition that reaches production is the one that read here, not the probe's list |
| 1b | Arms 3, 4 and 4b read the canary on the **path** route only | A dedicated keychain works, but only when the reader names it: the host's read path changes from keyring to a query by path (`read_at`'s shape). That is a design change for James and MASCHIN, not decided by this sitting |
| 2 | Half 3c is the one that read | The same, and every interpreter bump changes the partition, so it joins the T6 bump procedure |
| 3 | Arm 4 reads and arm 4b does not, or the other way round | The login state matters after all. Obi reads the facts of both before anything else |
| 4 | No half of arm 3 reads on either route | No route exists yet. MASCHIN ruled out a credential file, so this goes back to him and James, with the path route's OSStatus per half |
| 4b | Neither domain reaches the job's effective list (arm 2 says `none`) | The launcher cannot hand the host a dedicated keychain through the search list. The sitting goes on by path, and its result is row 1b or row 4 |
| 5 | Arm 7 reads the canary, or says anything but missing on a route | The probe cannot tell missing from locked on that route, so no arm counts on it. Stop and tell Obi |
| 6 | Step 15 does not end with exit 0 | The backup does not survive a reboot. That is a finding for MASCHIN tonight, independent of the probe, and the next night's run would fail too |

**Three things this cannot show.**

1. **"Nobody logged in" is not this machine's state.** `german` logs in by himself after each reboot. Arm 4 runs at boot, in a race against auto-login, and shows a boot start. Arm 4b shows the hourly job. Both stay in, because the FileVault decision may change which of them is production.
2. **The partition list is not in James's first table.** `add-generic-password` writes only Apple's partitions, so without it a failed arm 3 would say nothing about keychains. That an ad-hoc signed binary needs `cdhash:` rather than `unsigned:` is James's expectation, unmeasured until this run.
3. **The result holds for FileVault off.** Whether it goes on before the first real mail is the owner's decision (James on #97, section 3). If it does, auto-login goes off too, and arms 3, 4 and 4b run once more: about 10 minutes and one reboot.
