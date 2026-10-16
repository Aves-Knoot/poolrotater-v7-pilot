# v7.1 hub and private statistics publisher activation

This is a **staged maintainer procedure**, not an instruction to restart rooms.
Use this guide with the checksum-verified qualified v7.1 release kit; it is a
maintainer hub procedure, not an operator room-installation guide. Preparing or
extracting that release does not activate production. The package uses the exact
sealed v7.1.0 runtime; the currently prepared final2 digest is
`8c0971747c77a22a992749a6171363d60eb4a3bdc5f87460c55f88d881a21d35`.
Retarget only after a replacement candidate has passed its audit and qualification.

The existing registration/ratings hub has no in-place code-upgrade command.
This procedure exchanges only its protected code directory, preserving the
existing private hub state, TLS pair, server epoch, credentials, ratings,
enrollments, policies and operator choices. Statistics/log sharing remains off
unless each operator explicitly enables it. The website exporter is deployed
separately; this package provides its private, read-only network snapshot input.

Only `poolrotater-v7-hub.service` and the new statistics publisher need a brief
maintenance window. The active unit on this server retains the historical code path
`/opt/poolrotater-v7-hub-v706`; its name is not the runtime version. The verified
inventory is authoritative. The helper now verifies both the unit file and loaded
ExecStart before exchanging that exact code tree, so an unused older prefix
cannot be mistaken for the active service. The owner bot, community bots, rooms, catalog service,
release authority, website and analytics timer are not stopped. Hosts retain their
pending uploads while the hub is unavailable. Do not run a hub authority-reset
restore during this ordinary code upgrade: it would revoke existing enrollments.

## Read-only plan and sealed preparation

From the development checkout, the default action is read-only:

```bash
nice -n 15 /usr/bin/python3.12 -I -S -B scripts/prepare_v71_hub_transition.py plan
```

Record its `old_inventory_sha256`. The plan verifies every existing protected hub
code file and reports fixed paths. It reads no TLS key, OAuth token or private
player database. Staging requires the sealed artifact digest, explicit existing
public owner account ID, old inventory digest and a fresh private destination:

```bash
nice -n 15 /usr/bin/python3.12 -I -S -B scripts/prepare_v71_hub_transition.py stage \
  --runtime /root/poolrotater-v7.0/validation/v7.1.0-release-final2/runtime.zip \
  --runtime-sha256 8c0971747c77a22a992749a6171363d60eb4a3bdc5f87460c55f88d881a21d35 \
  --destination /root/poolrotater-v7.0/validation/v7.1.0-hub-transition-final2 \
  --old-inventory-sha256 REVIEWED_OLD_INVENTORY_DIGEST \
  --owner-account-id REVIEWED_PUBLIC_OWNER_ACCOUNT_ID
```

The already prepared package's exact values are in
`validation/v7.1.0/hub-transition-plan.json`, `hub-transition-stage.json` and its
private `transition.json`. Review those local files; do not publish the maintainer
package as an operator installer. It contains no copied production state or keys.

Staging validates the whole runtime hash, extracts only declared Python payloads,
and invokes that sealed source's existing `network.hub_service.prepare` in an
isolated interpreter. It does not build from a changing checkout. The resulting
`kit/runtime` has its own exact bootstrap inventory. `transition.json` hashes
all prepared code/assets and pins both existing and replacement inventories.

## Install reviewed auxiliary assets

The following commands are for a **later explicit activation**, after release
qualification and maintainer review. Do not copy placeholders literally. Set
`hub_transition_package` to the reviewed private package's absolute path:

```bash
hub_transition_package=/root/poolrotater-v7.0/validation/v7.1.0-hub-transition-final2
```

The destination code directory must not exist. If it exists, inspect its inventory
and any private transition receipts before proceeding; never delete unknown code.

```bash
sudo test ! -e /opt/poolrotater-v7-hub-v706.v710-next
sudo cp -a "$hub_transition_package/kit/runtime" /opt/poolrotater-v7-hub-v706.v710-next
```

Create only the dedicated analytics-read group, not another hosting account.
If any following unit/input/drop-in already exists, compare it with the reviewed
package and retain a private backup before an intentional replacement.

```bash
sudo install -d -o root -g root -m 0755 /etc/sysusers.d
sudo install -o root -g root -m 0644 "$hub_transition_package/assets/poolrotater-analytics.conf" /etc/sysusers.d/poolrotater-analytics.conf
sudo systemd-sysusers /etc/sysusers.d/poolrotater-analytics.conf
sudo install -d -o poolrotater-hub -g poolrotater-analytics -m 2750 /var/lib/poolrotater-v7-analytics
sudo install -d -o root -g root -m 0755 /etc/poolrotater-v7
sudo install -o root -g poolrotater-analytics -m 0640 "$hub_transition_package/assets/activity-publisher.json" /etc/poolrotater-v7/activity-publisher.json
sudo install -o root -g root -m 0644 "$hub_transition_package/assets/poolrotater-v7-activity-publisher.service" /etc/systemd/system/poolrotater-v7-activity-publisher.service
sudo install -o root -g root -m 0644 "$hub_transition_package/assets/poolrotater-v7-activity-publisher.timer" /etc/systemd/system/poolrotater-v7-activity-publisher.timer
sudo install -d -o root -g root -m 0755 /etc/systemd/system/poolrotator-wiki-analytics.service.d
sudo install -o root -g root -m 0644 "$hub_transition_package/assets/95-community-statistics-readonly.conf" /etc/systemd/system/poolrotator-wiki-analytics.service.d/95-community-statistics-readonly.conf
sudo systemctl daemon-reload
```

The publisher runs as the actual existing hub user with the dedicated analytics
primary group. It has no network, no access to the TLS key, strict filesystem
protection, bounded CPU/memory and a 90-second deadline. It exports every minute
through the protected `admin activity-export` entry. Its `After=` ordering does
not start the hub or any bot service. The input JSON contains only the fixed
private output path and reviewed public owner account ID.

The output folder is hub-owned **2750**, and each atomically published snapshot
is **0640** with inherited analytics group. The analytics service receives that
group and a read-only path; its mount namespace hides the entire private hub
state. It receives no database, credential or TLS-key access. Keep the private
snapshot outside the public web document root. Its rows include hidden identity
markers needed for suppression; only the website composer may turn it into
sanitized public aggregates. The analytics drop-in explicitly sets
`POOLROTATER_NETWORK_STATISTICS_SNAPSHOT=/var/lib/poolrotater-v7-analytics/activity.json`.
The separate website deployment must consume this exact private path. Timers
have no MainPID property; stopped admission checks MainPID only for services.

## Guarded code exchange

If newly installed publisher units are inactive, stopping them is harmless. Stop
only these named services; no bot or room restart is involved:

```bash
sudo systemctl stop poolrotater-v7-activity-publisher.timer
sudo systemctl stop poolrotater-v7-activity-publisher.service
sudo systemctl stop poolrotater-v7-hub.service
sudo /usr/bin/python3.12 -I -S -B "$hub_transition_package/transition.py" exchange \
  --package "$hub_transition_package" --confirm-stopped-hub
```

The helper refuses an active hub/publisher, an unconfirmed systemd state, a
remaining process under the hub UID, a changed inventory, an unsafe path, a busy
hub-server lease, special files, insufficient disk or a changed package. It
copies and verifies full private state and code under root-only
`/var/backups/poolrotater-v7-hub`, then uses Linux `RENAME_EXCHANGE` atomically.
After exchange the new code is at `/opt/poolrotater-v7-hub-v706`; the exact old code
remains at `/opt/poolrotater-v7-hub-v706.v710-next`. A receipt records both inventories.
The helper starts no service and never overwrites state or changes consent.

If interrupted, inspect both inventories and the private receipt before retrying.
The protected prefix is always one complete code tree. If inventories already
show new-at-prefix/old-at-next, the exchange happened: continue with checks rather
than blindly running exchange again. A failed pre-exchange check leaves the old
prefix untouched. Preserve private backups, TLS and state; do not paste them into
an issue or a chat.

Verify the sealed new code against preserved state, then start only the hub and
publisher. A failing check means retain state and inspect the private journal:

```bash
sudo runuser -u poolrotater-hub -- /usr/bin/python3.12 -I -S -B /opt/poolrotater-v7-hub-v706/bootstrap.py --root /var/lib/poolrotater-v7-hub check
sudo systemctl start poolrotater-v7-hub.service
sudo systemctl start poolrotater-v7-activity-publisher.service
sudo systemctl enable --now poolrotater-v7-activity-publisher.timer
sudo systemctl show poolrotater-v7-hub.service poolrotater-v7-activity-publisher.service poolrotater-v7-activity-publisher.timer -p ActiveState -p Result -p MainPID
sudo stat -c '%U %G %a %n' /var/lib/poolrotater-v7-analytics /var/lib/poolrotater-v7-analytics/activity.json
```

A fresh snapshot with no community activity is valid while statistics sharing is
disabled. Its existence is not proof that any real operator enabled collection,
that historical imports were reviewed, or that actual gameplay was accepted.
Verify the existing hosts reconnect with unchanged enrollments separately.

## Code-only rollback

If startup/readiness fails, or a qualified runtime issue requires reverting the
hub code, stop the publisher and hub, then invoke the same verified helper:

```bash
sudo systemctl disable --now poolrotater-v7-activity-publisher.timer
sudo systemctl stop poolrotater-v7-activity-publisher.service
sudo systemctl stop poolrotater-v7-hub.service
sudo /usr/bin/python3.12 -I -S -B "$hub_transition_package/transition.py" rollback \
  --package "$hub_transition_package" --confirm-stopped-hub
sudo runuser -u poolrotater-hub -- /usr/bin/python3.12 -I -S -B /opt/poolrotater-v7-hub-v706/bootstrap.py --root /var/lib/poolrotater-v7-hub check
sudo systemctl start poolrotater-v7-hub.service
```

Rollback requires the exact new/old inventory arrangement, makes another private
backup, and exchanges code atomically. It deliberately preserves newer state,
credentials, privacy choices and queued/history records. Leave the new publisher
disabled because old hub code lacks its export action. The website should report
stale community coverage while continuing to preserve owner history. Do not
rewind a live network database from a raw backup: that can undo revocation or
newer credentials. Disaster recovery uses the existing authority-reset restore
procedure and its explicit reenrollment requirements.

## Qualification evidence

Eight ordinary isolated tests exercise fixed service targets, publisher and
analytics unit boundaries, active-hub/process refusal, exact inventories,
malicious archive rejection, private backup safety and atomic exchange roundtrip.
`tests/v71_hub_publisher_permissions_fixture.py` runs the sealed final2 publisher
under actual distinct fixture UIDs/groups in the provisioning sandbox: hub UID
1000 publishes 0640 into a 2750 folder; analytics UID1002/group1001 can read the
snapshot and cannot write it or read the private hub TLS/database. It uses only
invented state and a canary key. A second provisioning test runs the complete
guarded upgrade and rollback against temporary protected code/state trees with
mocked inactive systemd observations and disk capacity; both inventories exchange,
private state remains byte-for-byte intact, and a newer state update survives
rollback. No service-start operation exists on this path. No production
activation or service restart is part of this evidence.

The hub code/publisher transition does not backfill operator history. Historical
public-room review and import is a separate explicit operator action; existing
owner statistics must not be described as globally shared until that import and
subsequent upload have been qualified.

## October16 activation checkpoint

The hub code and private publisher have now been activated on the actual unit's
protected prefix. The stopped code exchange preserved the entire existing private
state byte-for-byte; hub epoch, enrollment, TLS and operator choices were retained.
HTTPS capabilities and the0640 private snapshot passed live checks. The earlier
staging package remains an immutable historical receipt; do not replay it. The
current deployment package is private under
`/opt/poolrotater-v7-cutover-tools/v71-activation-20261016/hub-transition`.

The existing statistics page/exporter is also activated. Real owner-history volume
exposed excessive deep copying, fixed with copy-on-write native sets and bounded
pre-materialization ranking; all players still participate before top50 selection.
Thirty-eight existing website tests and an additional large-history memory/ranking
check passed, followed by actual private read-only profiling and successful live
publication. The analytics job retains nice15, with a384MiB memory cap,128MiB swap
cap,40% CPU quota and120-second deadline. Bot resource settings are unchanged.
No operator statistics/log sharing was enabled.

Owner7.0.7→7.1 is a separate one-time, installation-specific migration queue.
See V7_1_OWNER_ACTIVATION.md for its actual state and safeguards. It does not add
this storage migration to the ordinary signed updater or update another host.
