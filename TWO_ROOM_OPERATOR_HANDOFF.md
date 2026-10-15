# Dev12 — owner-tuned defaults and continuous two-room pilot

Download `poolrotater-7.0.1.dev12-live-pilot.zip` from the private GitHub release,
not GitHub's Source ZIP. Verify the archive hash in the release notes before
extracting, then run `sha256sum -c SHA256SUMS.txt` inside the extracted kit.
This replaces the earlier dev11 upgrade instructions for Xikeon's `two`.

Fresh installs now use the owner's saved gameplay defaults for each preset.
Explicit host ranges and saved per-room overrides retain precedence. Fresh
installation still defaults to ONE Standard Auto room; additional presets must
be explicitly chosen. Existing `two` is already Standard Auto + Mania 4K; retain
that topology. No reinstall, sign-in, invitation, grant, ratings migration or
catalog re-download is needed. Keep the older `first` instance stopped.

## Xikeon's existing installation

Your account is Xikeon (36451), instance `two`, public operator label Xikeon,
with existing enrollment and ratings sharing. Keep your chosen PoolRotator
Community titles. The maintainer explicitly requests replacing gameplay tuning
with their saved defaults; this is the one intentional configuration change.

- Standard Auto: empty/default range **4.00–5.00★**, bias **+0.70★**, half-width
  **0.35★**, pool **1,200**, minimum passes **10,000**, maximum map age **8 years**,
  stream share **50%**. Difficulty retunes with players.
- Mania 4K: fixed default range **3.00–4.00★**, pool **400**, minimum passes
  **1,000**, with the owner's style, farm and performance evidence tuning.
- Both: **15-second countdown**, freestyle, one queued map ahead, **45-second
  empty-room reset**, and the remaining saved gameplay/timing/voting defaults.

Do not interrupt occupied rooms for this release. If `two` is currently running,
arrange an empty/quiet window; use its protected `stop` once, verify actual
upstream room closure, then `network-stop`. Both units must be disabled/stopped,
with no surviving service processes and clean ownership. If ownership is
uncertain, retain state and follow REPAIR_UPGRADE.md for the actual instance/IDs
before proceeding. A filtered room list, elapsed time or 404 is not closure proof.

From the verified extracted kit:

```bash
# Existing dev10 runtime 199e4e228e833d6c:
bash UPGRADE_XIKEON.sh dev10

# ONLY if already upgraded to dev11 runtime 54b662a08bbfda59, use instead:
# bash UPGRADE_XIKEON.sh dev11
```

Local `start_allowed:false` with `startup_admission_not_evaluated_by_local_status`
is expected: startup performs fresh account/client/scope/room checks. A real
`recovery_required` state must be resolved before starting.

Choose one command matching the actual current registered runtime. Do not run
both. The script verifies checksums and stopped admission, captures private
before reports, upgrades the SAME installation, verifies upgrade preservation,
then applies the reviewed tuning and checks titles/authorization/catalogs/
enrollment/clean ownership before starting the network agent and bot ONCE.
Healthy rooms remain running. It stops on errors without a restart loop.
The new runtime is `53a1e98f0057a965cdbbb4d1afa4d1434296f91a7492e0d704fdf409717fae64` and its protected entry is:

```text
/opt/poolrotater-v7-pilot/two/releases/v7.0.1-53a1e98f0057a965/poolrotater_host/pilot_admin_entry.py
```

If already on dev12, do not rerun the upgrade script. Report the current status
and use the documented protected commands for that registered release. Other
origins require maintainer review rather than substituting a guessed old hash.

The settings helper requires explicit instance/account binding, verifies installed
code and registration, holds stopped ownership/network-admin guards, and backs
up only the previous host/gameplay configuration in a private directory under
`config/tuning-backup-<UUID>/`. It never edits OAuth, catalogs, enrollment, ratings,
journals or runtime source. Host and gameplay files are separately atomic writes,
not one transaction; on any failure keep stopped and inspect state/backups before
starting. Do not share those private backups.

## First checks and continuous running

Send both room links and redacted running support/network-support/ratings-support
JSON. Confirm titles, ranges, mode/keys and default tuning. Play 3–5 maps per room
when possible; try `.help`, queue, `.g`, `.b`, skip, Standard easier/harder,
countdowns, READY/unready/AFK with at least two players, and mid-match joins.
Never mark untested checks as passed. The hosting account should not join its own
rooms as a player; use other players for gameplay feedback.

Leave healthy rooms and the network agent running continuously. The first
60–90 minutes are focused checks, not a shutdown deadline. After the first hour,
send uptime, completed matches per room, any confusing responses and errors.
Thereafter a brief daily report is enough until a failure or next upgrade window.
Do not repeatedly poll expensive diagnostics or provoke outages in occupied rooms.
No automatic restart/enable policy is changed by this request.

For a NEW `.g`/`.b` vote, note map ID, direction and timestamp, then inspect ratings
support after the normal approximately 30–60-minute upload window. Report locally
captured, pending, accepted and rejection counts; acceptance must be confirmed by
the hub, not inferred from local capture. The two historical votes are already
accepted: do not recreate them. Do not reenroll or request another grant.

On a failure, retain all instance state and send safe failure code/HTTP status,
times/timezone, redacted support/network/ratings and a short relevant traceback.
If service has stopped, get ownership-review with the network agent also stopped;
resolve actual room ownership before restarting. Do not blindly retry startup,
delete journals/tokens, reinstall or recreate the instance. Clean-stop acceptance
can be checked at the next planned shutdown; it is not a reason to stop healthy
rooms now. Send no credentials, tokens, OAuth URLs/codes, invitations, private
pacing hashes, raw state, backups or unredacted player chat.

## Display and catalogs

The same full prepared Standard+DT, 4K+farm and 7K+farm catalogs are retained or
downloaded from the owner's VPS; research/player-score DBs and collection/analysis
stay on the owner's machine. Reduced file size does not mean sampled catalogs.

Dev11's chat ACK, queued chat-send, profile and score deferral repairs remain.
The compatible website is deployed. New v2 settings/progress observations still
require the maintainer's separate central-hub activation; old-hub fallback is
expected and not an operator failure. Do not install the hub on this container.
Map art/duration can resolve centrally without v2 timing. Progress, when present,
is Estimated and may hide for unknown timing or freestyle rate changes.
