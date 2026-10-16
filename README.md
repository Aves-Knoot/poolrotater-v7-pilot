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
