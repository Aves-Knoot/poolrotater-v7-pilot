> Current release: v7.1.1. Missing profiles now default to sharing and automatic updates; saved explicit choices remain. Read V7_1_1_OPERATOR_GUIDE.md first. The detailed explicit setup/opt-out and trust procedures below remain references; do not repeat enrollment or replace state merely to upgrade.

# Opt-in hub updates

The qualified v7.0.6 bootstrap and public release trust are being published for
the operator pilot. For Xikeon's existing dev14 `two`, use PILOT_UPGRADE_706.md
(the GitHub release asset is PILOT_UPGRADE.md) for exact paths, hashes and choices.
Dev14 has no updater. This general guide does not grant operator consent.

## One-time preparation

Keep your existing installation. Do not reinstall, repeat OAuth authorization,
replace catalogs, reset ownership or copy the owner's settings. The bootstrap
upgrade preserves saved configuration, authorization, Discord, titles, staff,
catalogs, enrollment, ratings, contributions and history. It also captures your
installation's inherited public gameplay defaults so later releases cannot
silently substitute another host's defaults.

The maintainer must supply:

- The manual GitHub bootstrap ZIP, exact archive/runtime hashes and supported
  predecessor hashes, with same-instance stopped-upgrade instructions.
- Your existing instance name and a clean stopped ownership review for that
  one-time upgrade. The updater itself does not close occupied rooms to bootstrap.
- The HTTPS executable-release origin, release-root JSON and its SHA-256, and
  the TLS CA PEM and its SHA-256, verified through a trusted separate channel.
- Confirmation that your existing hub enrollment is valid. The release trust
  root is separate from catalog trust and hub enrollment credentials.

Never send osu! or Discord credentials, token files or signing keys to the
maintainer. The updater reads local enrollment status and does not use remote
shell access or transmit your provider credentials.

After the supported bootstrap upgrade, set these to the instance and **release
path printed by that upgrade**, then verify local status:

```bash
pilot_instance=two
pilot_prefix="/opt/poolrotater-v7-pilot/$pilot_instance"
pilot_release=/opt/poolrotater-v7-pilot/two/releases/REPLACE_WITH_UPGRADE_RESULT
sudo /usr/bin/python3.12 -I -S -B \
  "$pilot_release/poolrotater_host/pilot_admin_entry.py" \
  --instance "$pilot_instance" status
```

Review the maintainer-supplied `release-updates.json`. It contains public trust
and explicit local choices; it must have exactly these fields:

```json
{
  "origin": "https://REPLACE_WITH_CONFIRMED_RELEASE_ORIGIN/",
  "ca_pem": "REPLACE_WITH_CONFIRMED_CA_PEM",
  "ca_sha256": "REPLACE_WITH_CONFIRMED_CA_SHA256",
  "bootstrap": {},
  "bootstrap_sha256": "REPLACE_WITH_CONFIRMED_RELEASE_ROOT_SHA256",
  "channel": "pilot",
  "automatic_download": true,
  "automatic_install": false,
  "controller_updates": false,
  "paused": false,
  "check_seconds": 1800,
  "report_to_hub": true
}
```

`bootstrap` must contain the complete confirmed release-root object. Empty
objects and example strings above are deliberately invalid. Do not use test
fixture roots, infer trust from a download alone, or reuse catalog-signing keys.

Choose `pilot` or `stable`. Automatic download, automatic installation, shared
controller maintenance and hub reporting are separate choices. The example
allows background downloads but leaves installation manual. To consent to
automatic worker installation, set `automatic_install` to `true`. To also consent
to coordinated controller maintenance, separately set `controller_updates` to
`true`. All policy booleans must be supplied explicitly.

```bash
sudo /usr/bin/python3.12 -I -S -B \
  "$pilot_release/poolrotater_host/pilot_admin_entry.py" \
  --instance "$pilot_instance" update-configure --updates-stdin < release-updates.json
sudo /usr/bin/python3.12 -I -S -B \
  "$pilot_release/poolrotater_host/pilot_admin_entry.py" \
  --instance "$pilot_instance" update-unit

up() {
  sudo /usr/bin/python3.12 -I -S -B "$pilot_prefix/updates-bootstrap.py" \
    --instance "$pilot_instance" --action "$@"
}
up status
up check
```

`update-unit` prepares disabled polling assets; it does not start polling. The
stable `up` entry follows protected registration after controller switches, so
you do not need to keep finding a new release directory.

## Manual controls and automatic polling

```bash
up download   # Explicit download and integrity verification.
up apply      # Explicit install request; native emptiness/ownership gates remain.
up status    # Local versions, progress, waiting rooms and held operation.
up pause     # Pause future automatic work.
up resume    # Resume under the choices already saved.
up start     # Enable/start independent background polling only after consent.
up stop      # Disable future polling; do not kill an in-flight controller commit.
up cleanup   # Receipt-bound retention; busy or suspicious inputs are retained.
```

The timer wakes independently of the bot. Saved `check_seconds` controls feed
polling with jitter and bounded backoff. HTTPS outages and failed checks leave
rooms running. Downloads are bounded, verified and resumed when supported.
While stopped, a host may check and download but the updater does not start rooms.

A worker-only release rolls one eligible room at a time through the existing
continuous-empty and late-join checks. Occupied rooms retain their running worker
and match. A controller/shared-runtime release requires the separate controller
choice, every configured room continuously empty, fresh ownership evidence and
private admission/drain checks. It may wait indefinitely for that safe window.
Private test profiles and other pending/failed deployments block unattended work.
Unsupported storage, interpreter or dependency migrations require a separately
reviewed manual migration; a channel opt-in does not authorize those changes.

To change consent/channel, edit only those choices in the original public trust
file and run `up configure --updates-stdin < release-updates.json`. Existing trust
cannot be silently replaced with a different root, CA or origin.

Pause prevents new submissions. An already accepted operation is reconciled
durably, rather than abandoned halfway through a room handoff or controller
transition. No uncertain provider mutation is replayed to make an update finish.

## After the one-time setup

Already installed v7.0.6 requires one local scheduling catch-up for its first
controller successor. See `pilot/AUTOMATIC_UPDATE_CATCHUP.md`; the helper retains
your settings and consent and uses the installed root updater. The maintainer
must qualify the repair and coordinate this one bootstrap action. Subsequent
compatible releases use normal polling alone.

With polling active and your chosen automatic-install/controller permissions
enabled, you do not need to fetch another GitHub ZIP or wait for the owner to
send instructions for a routine compatible release. Your host checks the signed
channel, downloads verified code, waits for the appropriate empty-room window,
and reports progress to the hub if you enabled reporting. Controller maintenance
may wait indefinitely while any configured room remains occupied. A paused,
stopped, download-only or worker-only installation keeps those choices.

Continue using the stable `up` command above after a controller switch. An update
held for uncertainty requires review; it is not an instruction to reinstall or
retry room creation. New trust roots and unsupported migrations remain separate
local decisions.

## Held operations

Save the `up status` output and the operation UUID. Keep state and journals.
Do not delete a failed deployment, reinstall, run ownership-clear speculatively,
or repeatedly request start to bypass a held update.

```bash
update_operation=REPLACE_WITH_EXACT_OPERATION_UUID
up recover --operation-id "$update_operation" --decision review
```

Review performs no service restart. Send the redacted result to the maintainer.
Only after the result identifies the supported resolution, use the same UUID:

```bash
up recover --operation-id "$update_operation" --decision retry
# Or the reviewed decision: cancel, complete or rollback.
```

`retry` is limited to a provably unsubmitted stage with an unchanged baseline.
`cancel` waits for native worker acknowledgment, or proves stopped clean ownership.

An interrupted candidate can leave a diagnostic `recovery_required` phase even
when its journal positively records `operation: closed`, no room/channel IDs and
`ownership_unknown: false`. If stopped cancellation is held for that reason, the
maintainer can review the stopped outputs and supply the exact current protected
release path. The verified bootstrap kit includes this explicit local repair:

```bash
sudo /usr/bin/python3.12 -I -S -B normalize_closed_parked.py \
  --instance "$pilot_instance" --release "$pilot_release"
up recover --operation-id "$update_operation" --decision cancel
```

The helper verifies the registered immutable runtime, stopped services, absence
of service-user processes and the held owner lock. It archives the original
diagnostics and changes only already-closed status fields. It refuses any owned
room, pending creation/close, unknown ownership or remaining room/channel ID;
it makes no provider request, clears no ownership and starts nothing. An older
release path is refused. Retain unknown ownership for the normal upstream
review/recovery workflow.
An already started roll must finish its native cleanup. A known compatible worker
activation failure can restore the prior worker; unknown ownership cannot.
Controller rollback is limited to aborting a prepared, stopped transition. A
committed or active controller is not automatically downgraded. Unsupported
decisions refuse while preserving evidence.

If a prepared registration transition prevents the ordinary admin entry from
loading, use the **exact protected candidate path from the retained controller
job**, confirmed with the maintainer. The dedicated candidate entry validates
its complete immutable inventory and environment before doing recovery:

```bash
candidate_release=REPLACE_WITH_CONFIRMED_PROTECTED_CANDIDATE_RELEASE
sudo /usr/bin/python3.12 -I -S -B \
  "$candidate_release/poolrotater_host/controller_update_entry.py" \
  "$pilot_instance" --operation-id "$update_operation" --decision review
```

## What to return from the pilot

Return redacted updater status before/after, timestamps, current room links,
whether rooms were occupied, versions before/after and whether an occupied match
continued. Include actionable failure codes and operation UUID if held. Confirm
saved titles/ranges/staff, authorization, catalogs, network enrollment, pending
ratings and contribution status survived. Do not send full state folders,
credentials, private configuration, player chat or player identities.

Hub update reports require your explicit reporting choice and authenticated
enrollment. They contain versions, channel, coarse transfer progress, waiting-room
count and closed failure codes, not player information or private settings.
Real player gameplay and long-running pilot acceptance remain separate from
isolated technical qualification. GitHub remains the manual recovery route.
