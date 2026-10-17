# Current release: v7.1.8

Use the [v7.1.8 installer release](https://github.com/Aves-Knoot/poolrotater-v7-pilot/releases/tag/v7.1.8), [operator guide](docs/V7_1_8_OPERATOR_GUIDE.md) and [maintenance procedures](docs/MAINTENANCE_OPERATOR_GUIDE.md). Supported controller-owned one-map/empty maintenance, cancellation, safer child startup, consecutive upgrade recovery and richer status preserve existing gameplay and data. Automatic controller updates wait for all configured rooms to empty; worker updates remain per-room. Stopped or recovery-required hosts need their local supported checks before one start attempt. Older repair launchers retain their old pins; use the current launcher for new attempts.

# Current release: v7.1.7

Use the [v7.1.7 live startup repair](https://github.com/Aves-Knoot/poolrotater-v7-pilot/releases/tag/v7.1.7) and [release notes](docs/V7_1_7_RELEASE_NOTES.md). It retains prompt shared feedback and repairs the live adapter timeout interface used by initial room creation and empty-room refresh. Existing settings, OAuth, enrollment, full catalogs, history and explicit opt-outs are preserved. Older repair launchers retain their original pins; use the current repair launcher for stopped hosts.

# Current release: v7.1.6

Use the [v7.1.6 installer release](https://github.com/Aves-Knoot/poolrotater-v7-pilot/releases/tag/v7.1.6) and [operator guide](docs/V7_1_6_OPERATOR_GUIDE.md). Map feedback keeps the familiar reply and updates shared totals promptly without double-counting across hosts. Automatic controller deployment waits for all configured rooms to empty and preserves settings, OAuth, catalogs, enrollment and opt-outs. The v7.1.5 repair launcher remains pinned to its original repair release.

# Current repair: v7.1.5

Use the [v7.1.5 installer release](https://github.com/Aves-Knoot/poolrotater-v7-pilot/releases/tag/v7.1.5) and [stopped repair/start guide](docs/V7_1_5_REPAIR_AND_START.md). The pinned launcher upgrades the existing stopped host, verifies recovery, and starts once only when requested. All existing settings, OAuth, catalogs, ratings, enrollment and opt-outs are preserved. Scheduled refresh now respects local cooldowns and retains replacement identity before confirmation. The paused maintenance/admin workflow is excluded. Signed registration leaves channel targets unchanged and does not queue other hosts.

# Current repair: v7.1.4

Use the [v7.1.4 installer release](https://github.com/Aves-Knoot/poolrotater-v7-pilot/releases/tag/v7.1.4) and [operator guide](docs/V7_1_4_OPERATOR_GUIDE.md). This expands default diagnostic sharing to DEBUG, private rooms, fixed service journals and early worker stderr, with credentials scrubbed. Existing opt-outs and configuration are retained. Stopped or failed hosts require a local ownership check and supported upgrade/start; publishing does not remotely restart rooms. The controller update waits for all configured rooms to be safely empty. The separate empty-room refresh interruption is still under investigation.

# Current room-worker update: v7.1.3

Running v7.1.2 controllers can apply the [v7.1.3 worker update](https://github.com/Aves-Knoot/poolrotater-v7-pilot/releases/tag/v7.1.3) one safely empty room at a time. See the [worker operator guide](docs/V7_1_3_OPERATOR_GUIDE.md). The shared controller stays on v7.1.2. For a fresh installation, use the v7.1.2 installer below; the v7.1.3 worker ZIP is not an installer kit. Paused and opted-out hosts keep their choices.

# Current repair: v7.1.2

Use the [v7.1.2 installer release](https://github.com/Aves-Knoot/poolrotater-v7-pilot/releases/tag/v7.1.2) and [operator guide](docs/V7_1_2_OPERATOR_GUIDE.md). This repairs score-fetch timeouts without resetting settings or history. Stopped or failed hosts require a local ownership check and supported upgrade/start; publishing does not remotely restart rooms. The separate empty-room refresh interruption is still under investigation.

# PoolRotater v7.1.1

Run your own lobbies using the retained native engine and complete centrally
prepared map-selection catalogs. OAuth/Discord credentials stay on your host.

Download the attached `poolrotater-7.1.1-live-pilot.zip`, verify its checksum from
this GitHub release, extract into a new directory, then run:

```bash
sha256sum -c SHA256SUMS.txt
```

Qualified platform: Ubuntu24.04 x86_64, Python3.12, systemd. A systemd LXC operator
also runs this platform. Optional official PP projection still needs the separate
.NET8 runtime. Use V7_1_1_OPERATOR_GUIDE.md for setup, opt-outs and actual feedback.

## Existing hosts

Already on exact v7.1.0 with the pilot updater enabled? Leave healthy rooms running.
The signed v7.1.1 successor waits for all configured rooms to be safely empty,
then performs one coordinated controller restart. A late join cancels admission.
Existing configuration, OAuth, Discord, catalogs, ratings, history, enrollment and
explicit opt-outs are preserved. This is not a storage migration.

Still on v7.0.6/7/8? First follow the one-time stopped migration in the
[v7.1.0 installer](https://github.com/Aves-Knoot/poolrotater-v7-pilot/releases/tag/v7.1.0).
Use its supported exact predecessor and retain all state and backups. This kit's
manual upgrade accepts the exact v7.1.0 runtime only; do not bypass the old storage
gate, reinstall, reauthorize, re-enroll or replace catalogs just to upgrade.

### Manual v7.1.0 successor upgrade

Follow V7_1_1_OPERATOR_GUIDE.md: save before support/settings, wait for all rooms
to empty, pause polling, cleanly stop bot and network agent, confirm clean
ownership and keep a private backup. Set the actual existing instance:

```bash
pilot_instance=YOUR_EXISTING_INSTANCE
sudo /usr/bin/python3.12 -I -S -B install.py --instance "$pilot_instance" \
  --bundle runtime.zip --sha256 d9bc7d722b8b9f20d0c16ad79503bd8e811c41653a3e745f01c95df8a29e9efe \
  --live-pilot --upgrade-from-sha256 8c0971747c77a22a992749a6171363d60eb4a3bdc5f87460c55f88d881a21d35
```

Use the newly printed protected entry afterward. Compare saved configuration,
authorization, catalogs, enrollment and history before restarting the prior
network/bot services and restoring updater intent. No room starts during install.
An uncertain/interrupted transaction requires its documented recovery, not a retry
with deleted journals. Keep all instance state.

## Fresh install

Verify all checksums first. The default is one Standard Auto room. For an explicit
two-room Standard Auto + Mania4K configuration:

```bash
sudo /usr/bin/python3.12 -I -S -B install.py --instance two \
  --preset standard-auto --preset mania-4k --bundle runtime.zip \
  --sha256 d9bc7d722b8b9f20d0c16ad79503bd8e811c41653a3e745f01c95df8a29e9efe --live-pilot
```

Follow NETWORK_OPERATOR_GUIDE.md, LIVE_AUTHORIZATION.md and the current operator
guide to import complete signed catalogs, set titles, authorize your own account,
and redeem a privately supplied bound hub invitation. No local collection/analysis
is needed. Do not run a second controller for the same hosting account.

## Enabled defaults and opting out

Before starting, review V7_1_1_OPERATOR_GUIDE.md. Missing profiles enable public
statistics sharing/consumption, sanitized operational/public-chat diagnostics,
enrolled rating contribution and verified shared rating selection. New directory
configuration defaults to public listing when omitted. Existing explicit false
values, independent readers and directory choices remain unchanged. The server
contribution grant is separate. Private/unknown-room content is excluded; raw
chat is never a public website resource. Historical data is not automatically
imported. Scrubbing, bounded retention and withdrawal receipts remain in place.

New updater configuration defaults to signed pilot downloads/installations,
controller updates and private update reports. A local `network-start` on an
enrolled v7.1.1 host initializes a missing updater from the packaged public root
and TLS pins. Existing trust/policies and stopped timers are not replaced. Use
`update-stop`, `update-pause`, or explicit `update-configure` preferences to opt out.
Publication does not prove installation, and polling never forces occupied rooms
closed or starts a stopped bot only to update it.

`network-reporting-configure` accepts all four explicit booleans on stdin:

```json
{"share_statistics":false,"consume_statistics":false,"operational_logs":false,"public_chat":false}
```

Keep the network agent running until central withdrawal is acknowledged. Shared
statistics consumption changes apply to newly constructed workers. Rating
contribution/reading remain separate supported settings. See the current guide.

## Useful feedback and qualification

Return redacted support, network/reporting/ratings/update status, version/hash,
room links and times. Try .discord, .help, .stats, feedback and completed maps.
Never post credentials, token files, private invitations, backups or unredacted
state. V7_1_1_VALIDATION.md distinguishes isolated qualification from real-player
soak. Website publication and independent operator activation are separate facts.
