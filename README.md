# PoolRotater v7.1.0 — shared statistics and private diagnostics

Technically qualified for manual installation and stopped upgrades on the tested
Ubuntu 24.04 / Python 3.12 x86_64 platform. Real operator gameplay/soak feedback
remains a post-activation check. Running production rooms were not changed by
this release preparation. This first storage migration is not on the auto-update feed.

The runtime SHA-256 is `8c0971747c77a22a992749a6171363d60eb4a3bdc5f87460c55f88d881a21d35`. Verify the separately supplied installer
ZIP checksum before extracting or executing it, then `sha256sum -c SHA256SUMS.txt`.
Use the installer asset, not GitHub's automatically generated source ZIP.

## What changes

The retained native room engine, settings, commands, Discord controls, full map
catalogs, local ratings, player history, enrollment and updater remain. v7.1 adds
separate opt-in player-statistics sharing/consumption and private diagnostic-log
sharing. Public chat requires another explicit choice. All four new reporting
choices start off. Existing directory, ratings and update choices are preserved.
No OAuth or Discord credential is sent to the hub.

The first v7.1 upgrade changes persistent-store code and therefore needs the
explicit stopped additive migration. It cannot be queued as an ordinary signed
worker/controller update. Once upgraded, the retained signed updater can deliver
future compatible releases normally: workers individually when empty, shared
controllers in coordinated empty-room maintenance. This kit does not enable any
update or reporting choice on an operator's behalf.

## Existing installations: one stopped migration

Only these exact predecessor artifacts are supported:

| Version | Runtime SHA-256 |
| --- | --- |
| v7.0.6 | `78e4befa5d56a9e28a9bb1640fd1da1c6282cfe0c68d7fc279e5a088ec0483eb` |
| v7.0.7 | `fd4f082e7b746dedc6d4e93d40eab6b1bc86f546daf50a9a6202d8cb01c23dcb` |
| v7.0.8 | `8a38ff2ec7c4a8dcb4b319d8a3981d96d7ec6fcc4d9efdf5d9b36539be4bd51d` |

Keep the SAME instance. Do not reinstall, reauthorize, apply another host's
defaults, replace catalogs, repeat committed ownership recovery or delete state.
Record the actual instance and release path from the current installation. The
examples use variables, not a guessed `first`/`two` instance.

```bash
pilot_instance=YOUR_EXISTING_INSTANCE
old_release=/opt/poolrotater-v7-pilot/$pilot_instance/releases/YOUR_CURRENT_RELEASE
oldpr() {
  sudo /usr/bin/python3.12 -I -S -B \
    "$old_release/poolrotater_host/pilot_admin_entry.py" --instance "$pilot_instance" "$@"
}
oldpr status
oldpr support
oldpr room-settings-status
oldpr network-support
oldpr ratings-support
```

Save these outputs privately for comparison. If an automatic update is already
committing, held or recovering, resolve its supported workflow before stacking a
manual migration. If v7.0.6's one-time catch-up helper is running, stop it
cooperatively first; do not kill a committing updater.

Wait for all rooms on THIS hosting account to be empty. For an updater-enabled
v7.0.6/v7.0.8 installation, record its prior updater state and run `oldpr update-stop`.
The owner's v7.0.7 baseline does not have that updater installed; skip that command
there. Then perform the normal clean stop:

```bash
oldpr stop
oldpr network-stop
oldpr ownership-review
```

Both the lobby controller and independent network agent run as the dedicated
service identity and must be stopped for migration. Each room must be positively
closed and locally clean. Retain state and report an uncertain result before
retrying; never remove journals or reset pacing to bypass ownership checks.

Set `previous_runtime_sha` to the actual matching predecessor hash in the table:

```bash
previous_runtime_sha=YOUR_MATCHING_PREDECESSOR_SHA256
sudo /usr/bin/python3.12 -I -S -B install.py --instance "$pilot_instance" \
  --bundle runtime.zip --sha256 8c0971747c77a22a992749a6171363d60eb4a3bdc5f87460c55f88d881a21d35 --live-pilot \
  --upgrade-from-sha256 "$previous_runtime_sha"
```

The upgrade preserves settings and authorization, creates the bounded additive
activity schema with a verified legacy-ledger backup, and starts no room. Keep
the migration receipt and backup. An interrupted transaction requires the same
installer's supported `--upgrade-status`/`--upgrade-recover` flow with its original
transaction UUID; do not install over it or invent another baseline.

Use the protected release path printed by the successful upgrade:

```bash
pilot_release=/opt/poolrotater-v7-pilot/$pilot_instance/releases/v7.1.0-8c0971747c77a22a
pr() {
  sudo /usr/bin/python3.12 -I -S -B \
    "$pilot_release/poolrotater_host/pilot_admin_entry.py" --instance "$pilot_instance" "$@"
}
pr status
pr support
pr room-settings-status
pr ownership-review
pr network-reporting-support
```

Compare topology, effective settings, catalog set, authorization, enrollment,
ratings and history with the saved before outputs. Do not start if migration or
ownership recovery remains pending. Existing hosts do not run configure_live.py,
authorize, catalog-import, network-enroll or ownership-clear just to upgrade.

## Owner hub first, community hosts afterward

1. The maintainer first upgrades the dedicated hub code and its separately staged
   website exporter through the existing guarded deployment procedures, preserving
   hub database, signing trust, host enrollment, votes and privacy policy. The
   operator installer does not replace the hub daemon or website automatically.
2. The owner migrates the same stopped three-room installation using the exact
   v7.0.7 predecessor above, preserves its existing OAuth/Discord/settings, checks
   the before/after evidence, then explicitly opts into the desired reporting.
   Intentionally disabled room configurations stay disabled.
3. Other operators migrate their own existing stopped installations with their
   own matching baseline. Reenrollment is unnecessary when enrollment is healthy.
   Their four reporting choices remain their decision. Existing rating-sharing
   and canonical rating-consumption choices remain independent.
4. After the hub's v7.1 endpoints are ready and local checks are clean, an operator
   can run `pr network-start` and `pr start` to restore their previous services.
   Restore updater polling only if it was enabled before the stop, or if the
   operator explicitly chooses it now. No empty-room activation is forced by
   publishing this kit.

The room engine continues operating if the hub is unavailable. Sharing retries
in the independent background agent with bounded storage. Collection never calls
osu!'s API and the `.stats` command does not perform network HTTP.

## Reporting choices and feedback

See V7_1_OPERATOR_GUIDE.md for consent, freshness, coverage and withdrawal details.
Existing history is not shared automatically: use the separately qualified
import_v71_public_history.py and V7_1_HISTORY_IMPORT.md only after explicit review.
V7_1_VALIDATION.md records tests, measured latency/resources and remaining live checks.
Configure all four choices explicitly on your own host:

```bash
pr network-reporting-configure < YOUR_PRIVATE_REPORTING_CHOICES.json
pr network-reporting-support
```

The exact stdin object has `share_statistics`, `consume_statistics`,
`operational_logs`, `public_chat` boolean fields. Keep it false unless you agree
to that purpose. Operational diagnostics include ordinary information, lifecycle
records and tracebacks; public chat/commands require public_chat as well.
Private-room content is excluded. Logs are private administrator diagnostics,
not a public website resource. Local limits are 7 days/32 MiB; central limits
14 days/64 MiB per host/512 MiB total, with explicit gaps when capacity expires.

Return redacted before/after support, reporting support, room links, test times,
version/hash and any fixed error codes. Check `.discord`, `.help`, `.help2`,
`.stats` self/other privacy, joins/leaves, completed maps, `.good`/`.bad`, and a
clean shutdown in a normal maintenance window. Do not send OAuth/Discord/hub
credentials, token files, raw private state, or unredacted logs by chat.

## Fresh hosts only

Qualified reference platform: Ubuntu24.04 x86_64, system CPython3.12 with venv,
systemd, and at least5GiB free disk reserve. The kit bundles27 pinned wheels;
the installer does not resolve/download dependencies or install OS packages.
Optional official PP projection requires the separately installed .NET8 runtime.
Other distributions/architectures/interpreters require their own qualification.

Only new hosts install fresh, import the full prepared catalogs, configure their
own OAuth client locally and enroll with an explicitly bound invitation. The
default fresh topology is one Standard automatic room. An explicitly chosen
two-room topology uses `--preset standard-auto --preset mania-4k`; upgrades never
change topology. Example fresh install, which starts no service:

```bash
sudo /usr/bin/python3.12 -I -S -B install.py --instance first \
  --bundle runtime.zip --sha256 8c0971747c77a22a992749a6171363d60eb4a3bdc5f87460c55f88d881a21d35 --live-pilot
```

Use the printed protected release path for catalog-import:

```bash
sudo /usr/bin/python3.12 -I -S -B "$pilot_release/poolrotater_host/pilot_admin_entry.py" \
  --instance first catalog-import --origin https://91.107.237.182:9445/ \
  --ca-file ca.crt --ca-sha256 60a0dac90855a1b0006062e9820c2144ba2240fc097cd685a6ca0b6fcb445f39 --trust-root bootstrap.json \
  --trust-sha256 2892f9fda822c2aade05fe5b28f354a57623b66555f75f2fb09fd4c0101f7a74 --mode standard --mode mania4 --mode mania7
```

These are full prepared selection databases and analysis companions, not the
central research/player-score databases. Catalog trust is separate from
executable-release trust. The bundled public updater profile is paused with
automatic installation/reporting off. Do not replace an existing saved updater
profile with it. New operators use configure_live.py, their local callback tunnel
and their own OAuth client only after reviewing LIVE_AUTHORIZATION.md and
NETWORK_OPERATOR_GUIDE.md. Never share those credentials with the maintainer.
