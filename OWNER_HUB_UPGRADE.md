# Community display pilot — dev11 upgrade

This kit includes the dev10 chat-pacing/shutdown repair, the follow-up repair
for Xikeon's reported ACK/chat-send failure, and public display observations
for map art, room settings, counts and estimated song progress.

Known transport loss or HTTP5xx for the idempotent chat keepalive now defers
maintenance instead of crashing gameplay. Persistent uncertainty counters and
the existing cooldown remain. Positively undispatched room-chat messages use a
bounded, ordered in-memory queue with expiry and room/channel binding. Player profile/top-score enrichment and post-match score
reads refused before dispatch also wait safely: roster/READY/gameplay observations
remain authoritative, and unknown PP does not become zero PP. Dispatched
uncertain message/room mutations, invalid authority, corrupt state and unsafe
child cleanup retain their existing safeguards. This is not general per-room
failure isolation; genuine fatal failures still trigger coordinated cleanup.

Family version remains v7.0.1; exact runtime SHA is 54b662a08bbfda597ae8db59f21e78a50f1dbf3fe266e7a8f9ca7991c2cef78f.

**Keep an ongoing test running. Do not interrupt rooms merely because this kit
exists. Arrange a clean, empty/stopped upgrade window with the maintainer.**
For full display parity, activate the central v2 hub first in its separately
agreed window. It is staged, not automatically deployed by this kit. Old
dev9/dev10 clients keep working with the new hub. A repair-only operator upgrade
can precede hub activation: a new client on the old hub falls back to v1 after
a confirmed read-only capability 404, without inventing settings or timing.

## Existing Xikeon `two`

Retain the existing installation, generation, OAuth, full catalogs, chosen
PoolRotator titles, room defaults, ratings, sharing and hub enrollment. No
new invitation/grant, sign-in, ratings migration or historical seed is needed.
Do not add presets, change room count, edit protected journals, or create a
second supervisor. Keep the older `first` instance stopped.

1. Capture redacted `status`, `support`, `network-support`, `ratings-support`
   and `room-settings-status` using the currently registered protected entry.
2. When players have finished and the room is empty, use `stop` once. Confirm
   actual upstream closure of both rooms and no bot workers. Use `network-stop`
   before stopped ownership review because the agent shares the service UID.
   Both units must be disabled and stopped as required by the installer.
3. Run `ownership-review`. If old ownership is uncertain, obtain fresh ended
   evidence and follow `REPAIR_UPGRADE.md`, using instance `two` and its actual
   IDs. Do not infer closure from elapsed time, a filtered listing or a 404.
   A journal with recorded ended IDs uses `ownership-clear --journal-sha256`
   with the fresh review hash; lost-creation association is a separate explicit
   case. A failed systemd unit with MainPID0 does not by itself block recovery.
4. Download the exact private release asset, independently verify its archive
   SHA against the release notes, extract into a new folder, and run
   `sha256sum -c SHA256SUMS.txt`. Do not use GitHub's source ZIP as the installer.

For a confirmed dev10 origin:

```bash
sudo /usr/bin/python3.12 -I -S install.py --instance two \
  --bundle runtime.zip --sha256 54b662a08bbfda597ae8db59f21e78a50f1dbf3fe266e7a8f9ca7991c2cef78f --live-pilot \
  --upgrade-from-sha256 199e4e228e833d6ce051d76b47817e558f261d0224d2cd9c88e3d7eadf0b7b81
```

The reviewed dev9 origin is
`b014b75a68fe9adb1612700f74e0038e3d689a7e71debde59518d41a469f10e1`;
the published dev7 origin is
`a340b0b719081c958853ec7435d90112588a26d348d46de057ce5bb9f8591932`.
Use the actual registered digest only if it is one of these reviewed origins.
Other origins require maintainer compatibility review, not a substituted digest.
The installer preserves state and starts nothing. Interrupted upgrades use its
documented upgrade-status/upgrade-recover transaction, not reinstalling.

Use the exact new release path printed by the installer:

```bash
pilot_release=/opt/poolrotater-v7-pilot/two/releases/v7.0.1-54b662a08bbfda59
pr() {
  sudo /usr/bin/python3.12 -I -S \
    "$pilot_release/poolrotater_host/pilot_admin_entry.py" --instance two "$@"
}
pr status
pr support
pr ownership-review
pr room-settings-status
pr ratings-support
pr network-support
```

Confirm clean ownership, preserved authorization/catalogs/titles/settings and
enrollment. Local `start_allowed:false` with
`startup_admission_not_evaluated_by_local_status` is expected; start obtains
fresh identity/client/scope/room checks. Recovery-required is a different issue
and must be resolved before starting.

Pending votes must remain captured. The maintainer may coordinate one
`ratings-sync` while the agent is stopped and verify central acknowledgment;
do not recreate votes or repeatedly flush the backlog. Shared map weighting
remains the operator's existing choice; this display upgrade does not change it.

Start the existing network agent and bot once in the agreed window, using
`network-start` and `start`. Return both links and active support. Keep full
catalog trust/cache; importing another copy merely for the upgrade is unnecessary.

## Website acceptance

The website centrally resolves map art and duration from complete prepared
metadata. Operators do not download research databases or run collectors.
Legacy clients can get art and duration without new timing. Settings and
progress require fresh v2 observations after the new runtime and hub activate.

Play several maps in both rooms. Check homepage, Live Lobbies and sidebar for:

- Correct host attribution, title, mode/keys, range, map and official cover.
- Occupants labeled separately from explicitly observed playing people.
- Public settings matching the room; no passwords or controls for another host.
- Progress marked **Estimated**, beginning after actual match start and clearing
  after completion/abort, map replacement, room recreation or stale observations.
- Mobile layout, working joins, and retained expanded settings during refresh.

Progress is based on observed events and known constant clock rate, not an exact
upstream game clock. Freestyle, optional speed changes, unknown timing or missing
metadata can legitimately hide the estimate. Never mark untested features passed.
Return map IDs, test times/timezone, screenshots, completed checks and safe errors.
Real remote timing accuracy remains gameplay acceptance, separate from simulated
installed/schema/browser qualification.

## Failures and rollback

Retain state and report uncertain startup/shutdown before retrying. Do not clear
tokens, journals or queued ratings to fix display. Hub/display outages must not
stop gameplay; expired community joins and timing are withdrawn.

Website rollback is independent of bot upgrades. Hub code rollback preserves
the old directory shape through the internal display extension and requires a
coordinated stopped hub window. Do not downgrade operator runtimes or remove
new pending heartbeat records manually; use a reviewed transition/recovery path.
No automatic migration, service activation or owner cutover is performed.

The harmless `ai_context_file=''` startup warning remains a known minor issue.
Record it separately from chat failure codes; this release does not claim to fix it.
