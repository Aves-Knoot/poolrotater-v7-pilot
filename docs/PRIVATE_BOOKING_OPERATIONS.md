# Private bookings: current operator workflow

Status: source, exact-package browser, and installed donor qualification passed.
Frozen unpublished v7.3.0 runtime SHA-256 is
`fa15a041bb3ae7469ff668fc306f69b8b61e63c46a4e43dd447a32cbfe1cef92`, from
source commit `1a713e9` (prior `6cbf86d`), superseding `1c4ca...` and `67d545...`.
The unpublished host/hub kits have verified inventories, checksums and reproducible archives; see `../candidate/HANDOFF.json` and `ARTIFACT_VALIDATION.json`. No production portal,
OAuth client, service unit, proxy route, private slot, or live room has been
configured. Keep both booking opt-ins disabled until a separate human review
authorizes a pilot.

The implementation has two explicit opt-ins. The installed managed host must
enable one or more private aliases with `managed-bookings`; the central hub
must separately have an enabled local portal configuration before it serves
the browser UI. Missing configuration is disabled by default in both places.
Neither step creates a room or installs or starts a service.

## Protected hub entry points

The installed standalone hub is launched through its pinned `bootstrap.py`
entry point with the system interpreter's isolated mode (`-I -S`). Bootstrap
verifies the installed inventory before importing hub code. It exposes these
booking actions:

* `booking-check` validates `bookings-site.json` locally and prints only
  `enabled`, origin, loopback bind, port, and whether OAuth fields are present.
  If the file is absent, the portal remains disabled. This does not contact
  osu!, initialize browser-session storage, or start a listener.
* `booking-serve` enters the portal launcher. It binds only to `127.0.0.1`,
  refuses disabled configuration, and starts no listener unless the local
  `enabled` value and OAuth configuration pass validation.
* `booking-slots` accepts bounded JSON on stdin to list or configure hub-side
  slots. Its response explicitly reports `rooms_started: false`,
  `bookings_created: false`, and `services_changed: false`.

The portal file is `<hub-root>/bookings-site.json`, separate from `hub.json`.
The reader accepts a private regular file owned by the hub service identity,
with no group or other permission bits. No public HTTP route changes this
file. Its current default is:

```json
{
  "format": 1,
  "enabled": false,
  "origin": "https://poolrotater.com",
  "bind": "127.0.0.1",
  "port": 9446
}
```

An enabled site config requires a numeric OAuth client ID and its secret. The
callback is fixed to
`https://poolrotater.com/private-lobbies/oauth/callback`; OAuth requests use
the `identify` scope. Optional `preview_catalog_root` is an absolute path
chosen only in this private local file. Browser requests cannot select catalog
paths. Registering OAuth credentials, adding a production reverse-proxy route,
and installing or starting `pilot/bookings-portal.service.template` are future
human-gated actions; this document does not authorize them.

## Installed host opt-in

The already-installed pilot administrator has a protected
`managed-bookings --managed-stdin` action. The input is limited to this shape:

```json
{"format":1,"enabled":true,"aliases":["room1"]}
```

Aliases are limited to `room1` through `room4`. Enabling an alias requires the
existing native room to be private, stopped, free of pending or failed native
work, and confirmed closed by the ownership journal. The policy change is
written atomically. The command reports `provider_dispatch: false`; it does
not start a room, change a native room service, or dispatch a provider action.
Enabling the policy for the first time reports that the already-running
network agent needs one restart before booking exchange begins.

The browser portal's hub-side slot list is a separate local setting, passed to
`booking-slots` on the hub. It binds a hub slot ID and safe display label to an
eligible enrolled host alias, a preset list, and validated base settings. A
slot is publicly reservable only while its host report is fresh and the host
reports the matching private alias and supported preset. Slot configuration
does not bypass the installed host opt-in.

## Portal access and controls

The public page displays only a slot label, public availability state,
supported presets, and a separate freshness indicator. It does not show room
IDs, passwords, player names or IDs, join links, current maps, or queue entries.
The portal uses same-origin API requests, a host-only secure session cookie,
and CSRF tokens for mutations.

The owner and controllers receive the private settings panel, current map,
bounded next-map queue, and player roster. Both roles can start rotation,
abort or skip the current map, and configure schema-supported settings. Only
the owner can end a booking, create an invite, or grant/revoke controller
access. A friend who claims the room remains a player until the owner grants
controller access. Invited players can see joining details but do not receive
settings or roster controls. Join URL and password are returned only with an
authorized booking role; the public slot card never contains them.

Settings controls come from the selected preset's server inventory. Common
controls include star range, map length and rotation timing, style and mod
choices, and boolean toggles when that preset supports them. Standard
auto/fixed presets additionally expose DT mode (`auto`, `off`, `mixed`, or
`full`). The website keeps edits as a draft until the user explicitly applies
them; the server enforces revisions and a shared ten-second apply cooldown.
Receipts distinguish queued work, live application, next-map application and
failure. The player allowlist has no control that requires room recreation.

Selection filters expose only metadata-qualified AR and map length. Map length
is available for all supported booking presets. AR is unavailable for Mania
and the currently qualified Catch catalog. OD and BPM are unavailable because
the complete catalogs do not qualify those metadata fields; the UI explains
unavailable filters instead of inferring values. Hard ranges exclude maps,
while preferred ranges affect selection weight without excluding maps.

## Upgrade and rollback behavior

The existing managed update gate holds updates while a booking lease remains
active. Unreadable or uncertain booking state also holds the update rather
than releasing an alias. The established stopped-host reconciliation path
retains booking lease identity and performs verified cleanup; do not edit the
booking databases, clear an alias, or stop a room manually to get past a hold.

For an approved immediate rollback, use feature-off: stop new reservations by
disabling the portal config, then stop the portal process through its reviewed
service-manager procedure. Keep the booking ledger and host state intact while any
booking or cleanup is active or uncertain. The host `managed-bookings` policy
cannot release an alias held by an active lease; disable or remove aliases only
after cleanup is confirmed. Restarting the existing host agent is needed when
the managed booking policy changes from disabled to enabled. Rollback does
not require deleting session or booking history. A binary downgrade is
separately reviewed and requires a stopped transition or a consistent
closed-state backup; do not blindly downgrade or overwrite active ledgers.

## Local validation

The private portal configuration check and package/DOM qualification are
offline development checks. They do not install a service, create a site route,
register credentials, access a production catalog, or create a live room. See
[`PRIVATE_BOOKING_UI_VALIDATION.md`](PRIVATE_BOOKING_UI_VALIDATION.md) for the
qualified test commands and the DOM fixture's browser limitations.

The frozen runtime's exact-package Chromium portal run passed 1 test in
33.950 seconds. The installed donor upgrade preserved 63 files and 64
database-table rowsets, including synthetic ratings/player history, and did
not start a service. The current full-system donor receipt passed 9 native
lifecycle cases, 3 simulated browser HTTP cases, 1 authenticated TLS
portal-to-hub/agent/coordinator/engine integration, 1 real database guard, and
all 6 preset previews across 5 full-mode catalogs. It ran privately with no
real provider or rooms. These receipts do not authorize production activity.

## Measured portal memory budget

The current installed donor run with the full five-mode catalogs and all six
preset previews peaked at740,156KiB RSS while also loading native/browser test
helpers. The optional portal unit therefore reserves a1GiB memory ceiling,
with two cached pools,50%of one CPU andNice19. The earlier512MiB template
was not sufficient evidence of a qualified budget and is superseded. Review
available hub memory before the approved pilot; community hosts do not run
the browser preview process. This measurement is synthetic qualification, not
an observed production portal load or a multi-user load-test result.

## Staged rollout after separate approval

1. Keep both opt-ins off. Retain consistent hub/host backups and record installed
   hashes, desired stopped slots, room defaults, capacity and ownership status.
2. Upgrade the hub with the additive kit first. Old hosts remain ordinary hosts;
   the booking endpoint and browser service do not grant them booking capability.
   Preserve the exact-version v7.2.14 fleet rollout gate from the catalog
   rollout. Mixed-version hosts may wait at that gate until eligible; do not
   bypass it to complete the booking rollout.
3. Upgrade hosts through the established signed controller-update admission.
   The candidate is a controller release: it waits for all native boundaries,
   and any reserved/active/unclean booking holds the whole host update. Verify
   config, enrollment, credentials, catalog set, ratings and history are retained.
4. Configure the separate identify-only site client locally, review proxy/log
   settings, install the optional portal unit and run protected booking-check.
   Provide complete trusted exports owned by the hub identity for previews;
   browser requests cannot select filesystem paths. No research/player DBs are
   distributed or analyzed by this process.
5. Select only a deliberately unused, confirmed stopped private alias. Apply
   the installed host booking policy through bounded stdin, then arrange the
   single independent network-agent restart required for first enablement.
   Native public aliases and existing room defaults must remain unchanged.
6. Bind the central slot with booking-slots, safe label, allowed presets and
   validated base settings. Confirm capacity, fresh generation-bound reports
   and disabled/unavailable/reserved displays before opening reservations.
7. Conduct the separately authorized one-slot pilot in PRIVATE_BOOKING_PILOT.md.
   Require confirmed closure, password/settings/role cleanup and successful
   reuse before approving public availability. Preserve every uncertain record.
8. Public release signing, update-feed promotion and website/wiki publication
   remain separate explicit owner decisions; the local candidate does none.

Immediate supported rollback is feature-off: disable new reservations and the portal while retaining
all host/hub ledgers. Resolve/finish existing sessions through the current code
before removing designation. Do not blindly downgrade or copy old DBs over
active state. A binary rollback needs a separately reviewed stopped transition
or a consistent closed-state backup; retaining the current runtime with the
feature disabled is the supported immediate rollback. Binary downgrade is
outside this qualification; it requires a separately reviewed stopped
transition or consistent closed-state backup. Existing hosting OAuth and
catalogs need no reset.

## Future enablement input examples

These are review examples for the approved rollout, not instructions to run
before authorization. Run the existing protected host operator entry point as
administrator; keep its instance binding and existing OAuth unchanged. Supply
the host policy on bounded stdin:

```json
{"format":1,"enabled":true,"aliases":["room3"]}
```

The action is `managed-bookings --managed-stdin`. It refuses a public, occupied,
uncertain or undesignated alias; it never creates a room. Do not disable/remove
a designation while a lease is held.

On the hub, use the protected bootstrap as the hub service identity:

```sh
sudo -u poolrotater-hub /usr/bin/python3.12 -I -S /opt/poolrotater-v7-hub/bootstrap.py --root /var/lib/poolrotater-v7-hub booking-check
```

For `booking-slots`, pass one of these JSON documents on stdin. Replace the
invented host UUID with the real enrolled host ID from its approved handoff:

```json
{"format":1,"action":"list","slots":[]}
```

```json
{
  "format":1,
  "action":"configure",
  "slots":[{
    "slot_id":"private-west",
    "host_id":"aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
    "alias":"room3",
    "label":"Private lobby",
    "presets":["standard-auto","standard-fixed"],
    "base_settings":{"sr_min":4.0,"sr_max":5.0},
    "enabled":true
  }]
}
```

The same bootstrap path accepts `booking-slots` instead of `booking-check`.
A configured slot stays unavailable until the exact host generation advertises
fresh compatible catalogs, matching private policy and native capacity. A room
is not reserved by these commands; only an authenticated, authorized browser
reservation obtains a lease. Keep automatic updates and existing hosting
settings as they were; booking holds are added to ordinary update admission.
