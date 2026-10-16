# Existing operator installation: one-time dev14 → v7.0.6 updater bootstrap

Use the SAME `two` installation and keep `first` stopped. Preserve both rooms,
chosen titles/ranges, all settings, OAuth/Discord, full catalogs, enrollment,
rating sharing, pending votes and history. Do not reinstall, authorize again,
apply the owner's defaults, reimport catalogs or repeat committed recovery.

The maintainer's release hub is ready. Installation on your machine and each
update-consent choice still require your local approval. An assistant following
this document must not infer your consent from the maintainer's authorization.

## 1. Download and verify

From the release:

https://github.com/Aves-Knoot/poolrotater-v7-pilot/releases/tag/v7.0.6-updater-bootstrap

Download these assets, not GitHub's source ZIP:

- `poolrotater-7.0.6-updater-bootstrap.zip`
- `release-updates.json`
- `release-ca.crt`
- `release-root.json`
- `PILOT_UPGRADE.md` (this guide)
- `SHA256SUMS.txt`

Independently confirmed hashes:

| File | SHA-256 |
| --- | --- |
| Bootstrap ZIP | `bcb6c83f1c7254ff13c99191b6a6cbdd74c63fddcd614e4f191098f8258276ff` |
| Public trust/config JSON | `39b796765729fab395cd45e16130f61d125bb3b985cbacc960c815e556a9b20e` |
| Release TLS CA | `58ec13427d16a40aaac091c6d64aa531a295e67a939a6cb77fa977e8b673c35e` |
| Executable release root | `d88125b6a8406d607307ee9403989b30b02331f2335b9a2bad8c49ff64dff027` |

Confirm these against the maintainer's separately sent values. In the download
directory, verify the downloaded checksum list and the explicit ZIP hash:

```bash
sha256sum -c SHA256SUMS.txt
printf '%s  %s\n' \
  bcb6c83f1c7254ff13c99191b6a6cbdd74c63fddcd614e4f191098f8258276ff \
  poolrotater-7.0.6-updater-bootstrap.zip | sha256sum -c -
mkdir kit-706
unzip poolrotater-7.0.6-updater-bootstrap.zip -d kit-706
cd kit-706
sha256sum -c SHA256SUMS.txt
```

Keep the public `release-updates.json` in the parent download directory. It is
different from the deliberately invalid example JSON inside the kit. The kit's
embedded `published:false` is its historical packaging status; this release and
this guide are the current publication/activation instructions. The frozen kit
bytes were retained exactly as qualified.

## 2. Capture before state; wait for both rooms to empty

This exact manual upgrade accepts dev14 runtime
`182615d79647a68dfca9f75ed440b77081dee52682a7d1bc737e913fd36bbd96`.
If your actual installation differs, send its version/hash and stop here. Do not
substitute the hash or redirect this command to `first` or another installation.

```bash
old_release=/opt/poolrotater-v7-pilot/two/releases/v7.0.1-182615d79647a68d
oldpr() {
  sudo /usr/bin/python3.12 -I -S -B \
    "$old_release/poolrotater_host/pilot_admin_entry.py" --instance two "$@"
}
oldpr status
oldpr support
oldpr room-settings-status
oldpr network-support
oldpr ratings-support
```

Save those outputs privately for comparison. Leave healthy rooms running until
the agreed normal empty maintenance window. Then:

```bash
oldpr stop
oldpr network-stop
oldpr ownership-review
```

Both old rooms must be positively closed and the stopped review clean. If a
command fails, ownership is unknown, or a room remains open, retain everything
and send the redacted result before retrying. Do not delete journals, reset
pacing, kill processes manually or force an ownership-clear. The installer also
checks that units/processes have stopped.

## 3. Upgrade the same stopped installation

From `kit-706`:

```bash
sudo /usr/bin/python3.12 -I -S -B install.py --instance two \
  --bundle runtime.zip \
  --sha256 78e4befa5d56a9e28a9bb1640fd1da1c6282cfe0c68d7fc279e5a088ec0483eb \
  --live-pilot \
  --upgrade-from-sha256 182615d79647a68dfca9f75ed440b77081dee52682a7d1bc737e913fd36bbd96
```

Expect `operator_state_preserved:true` and `service_started:false`. No presets or
tuning step should be supplied. Confirm the exact release path printed by upgrade;
the expected path for these bytes is:

```bash
pilot_instance=two
pilot_prefix=/opt/poolrotater-v7-pilot/two
pilot_release=/opt/poolrotater-v7-pilot/two/releases/v7.0.6-78e4befa5d56a9e2
pr() {
  sudo /usr/bin/python3.12 -I -S -B \
    "$pilot_release/poolrotater_host/pilot_admin_entry.py" \
    --instance "$pilot_instance" "$@"
}
pr check
pr status
pr support
pr room-settings-status
pr ownership-review
pr network-support
pr ratings-support
```

Confirm your Standard Auto and Mania4K presets, titles, ranges, offsets,
countdowns, staff, catalogs, authorization, enrollment, pending ratings and sharing
match the before state. The version changes. `start_allowed:false` from local
status alone can be normal: startup performs fresh admission checks.

Start normally ONCE:

```bash
pr network-start
pr start
pr status
```

Send both new room links and the start time/timezone. A failed or uncertain start
requires preserving/reporting state before any retry. Keep healthy rooms running.

## 4. Configure public release trust, initially paused

The executable-release origin is `https://91.107.237.182:9446/`. This is separate
from catalog downloads and the existing enrolled hub. Its public configuration
does not contain provider credentials, invitations or signing private keys.

Verify the exact public file before first configuration:

```bash
printf '%s  %s\n' \
  39b796765729fab395cd45e16130f61d125bb3b985cbacc960c815e556a9b20e \
  ../release-updates.json | sha256sum -c -
pr update-configure --updates-stdin < ../release-updates.json
pr update-unit
up() {
  sudo /usr/bin/python3.12 -I -S -B "$pilot_prefix/updates-bootstrap.py" \
    --instance "$pilot_instance" --action "$@"
}
up status
up check
up status
```

The supplied policy selects `pilot`, is paused, and disables automatic downloads,
installation, controller maintenance and private reports. Configuration and unit
preparation start no updater. `up check` explicitly checks signatures/TLS without
installing anything. At initial publication the pilot head is the SAME v7.0.6
runtime just installed, so no available successor is expected. This is a normal
result, not a missing database or failed download.

## 5. Enable only the choices you approve

For the proposed unattended pilot, explicitly approve all of:

1. Periodic signed release checks and automatic downloads on `pilot`.
2. Automatic compatible worker updates after each room safely empties.
3. Shared-controller updates only after every room passes the continuous-empty,
   admission-lock and drain checks.
4. Private coarse update reports (versions, phase, counts and fixed error codes).

Public directory consent and rating sharing stay as previously configured.
If you approve those four choices, create a separate local consent file:

```bash
python3.12 -I -S - <<'PY'
import json
from pathlib import Path
source = Path('../release-updates.json')
data = json.loads(source.read_text())
data.update(automatic_download=True, automatic_install=True,
            controller_updates=True, report_to_hub=True, paused=False)
Path('../release-updates-consented.json').write_text(json.dumps(data, indent=2) + '\n')
print({key: data[key] for key in ('channel', 'automatic_download',
      'automatic_install', 'controller_updates', 'report_to_hub', 'paused', 'check_seconds')})
PY
```

Review those printed choices. If you want download-only/manual installation,
leave the corresponding install/controller/report flags false in that local file.
Do not change the supplied origin, CA, root or their hashes. Then apply the
approved choices and start independent polling:

```bash
pr update-configure --updates-stdin < ../release-updates-consented.json
up check
up start
up status
```

Checks are scheduled about every1800seconds with jitter/backoff; timer wakeups
and installation-progress checks are more frequent. Worker updates wait for
eligible empty rooms. Controller updates wait for all rooms to be safely empty.
Do not empty rooms artificially to rush the pilot. A hub outage leaves rooms
running. An uncertain lifecycle operation holds for supported recovery.

`up pause` pauses future submissions; `up resume` restores saved choices.
`up stop` disables future polling. None is permission to kill an already accepted
controller commit or erase its journals. The stable `up` launcher follows future
controller paths, so it remains valid after updates. Use GitHub as the manual
fallback; unsupported dependency/schema migrations can still need manual work.

## 6. Return results and leave healthy rooms running

Please send:

- Before/after redacted `support`, `network-support`, `ratings-support` and
  `room-settings-status`, with private staff/config/player fields removed.
- `up status` and the post-configuration `up check` result, including `hub_report`
  if reporting was approved. Confirm the consent choices you enabled.
- Upgrade/start times, both room links, and confirmation that saved settings,
  catalogs, authorization, enrollment, sharing and pending votes survived.
- After several hours: gameplay/log summary, failures, unexpected closures and
  whether contributions still reach the hub.
- When the next genuine signed release arrives: versions before/after, operation
  UUID, whether occupied matches continued, rooms waited for emptiness, and the
  final update status. That first real-use delivery acceptance remains pending;
  the maintainer will not publish synthetic QA releases for your rooms.

For a held operation, return the fixed error code, timestamp and read-only review:
`up recover --operation-id EXACT_UUID --decision review`. Retain state and wait
for guidance before a retry/complete/cancel/rollback decision. If one room parks,
leave its healthy sibling running. Never send credentials, token files, raw state
databases, private configuration, pacing-state hashes, player chat or identities.
