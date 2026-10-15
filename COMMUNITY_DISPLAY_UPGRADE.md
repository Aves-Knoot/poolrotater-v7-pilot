# Dev13 playlist and pacing repair — existing dev12 `two`

Keep the same installation, OAuth, catalogs, titles, ranges, settings, hub enrollment,
sharing and local ratings. Do not reinstall, sign in again, repeat ownership-clear,
create votes again, or apply owner defaults again. Keep `first` stopped.

Download the attached `poolrotater-7.0.1.dev13-live-pilot.zip` from the private
GitHub release, not GitHub's Source ZIP. Extract into a NEW directory. This repair
uses the v7.0.1 native runtime family; dev13 is the package version.

## 1. Clean stopped window

This script is for Xikeon's already stopped dev12 `two` (both rooms local_clean).
If healthy rooms are running, leave players undisturbed and arrange a clean,
empty-room stop through the existing protected entry first. Do not force a stop
or discard ownership state. Both bot and network units must be stopped and disabled.
The script checks this and refuses otherwise. A stale failed unit with MainPID 0
is accepted; no manual reset-failed is required.

## 2. Upgrade and start once

From the extracted kit as the normal operator:

```sh
sha256sum -c SHA256SUMS.txt
bash UPGRADE_XIKEON.sh dev12
```

The script verifies the kit, collects private before reports, checks both local_clean
rooms, upgrades only from the exact dev12 runtime, checks saved room settings are
identical, and starts the network agent and bot once. It never retunes the instance.
The installer preserves all existing operator state. No collectors or analysis run
locally; the same complete prepared catalogs remain in use.

New runtime SHA256: `ad33e8eea1ca5dcd8367d6bce7658ba21e6a84432d271b2dbe979420f6a11ec8`

New protected entry:
`/opt/poolrotater-v7-pilot/two/releases/v7.0.1-ad33e8eea1ca5dcd/poolrotater_host/pilot_admin_entry.py`

If any step fails, retain the instance and report folder, send the redacted error,
and stop there. Do not rerun startup blindly. Reports contain account IDs and private
hashes: redact them before sharing. Never send credentials or token/state databases.

## 3. Useful real-use acceptance

Send both room links and start time, redacted support / network-support /
ratings-support, and whether existing titles and ranges were preserved. Let healthy
rooms run continuously. Test normal joins, READY/unready/AFK, countdown, map progression,
`.help`, `.easier`/`.harder`, `.skip`, `.start`, `.good`/`.bad`/`.ok` when convenient.
For the specific repair, in Standard request `.freedomdive`, skip to it while idle,
and confirm it can start and finish while 4K continues normally. Do not abort an
occupied match just for this check. This hidden command remains unadvertised publicly.
Its existing shared cooldown is preserved. If the earlier test still has it cooling
down, report the remaining time and test later; do not clear or reset the cooldown.

The repair recognizes only the explicitly queued built-in Approved normal-speed
chart, binds pending playlist IDs after validation, and backs off inconclusive
liveness probes. Unknown or mismatched speed/ruleset variants remain refused. A
late chat response during explicit shutdown no longer marks a worker crash.
General worker fault isolation is still future work; this is not a claim that all
provider failures are recoverable.

If anything stalls, capture time, command, both room links, redacted support and
relevant journal lines including failure code/HTTP status. Retain state; check
ownership before retrying. No need for constant manual reports when healthy: a
summary after several hours plus any incident is enough. Pending ratings remain
local until upload acknowledgment; do not recreate them.

Short `.help` and clearer `.good` / `.bad` feedback prompts are included. Optional
PP projection still needs the separately installed .NET 8 runtime; its absence does
not explain this playlist jam and .NET is not bundled in this repair.
