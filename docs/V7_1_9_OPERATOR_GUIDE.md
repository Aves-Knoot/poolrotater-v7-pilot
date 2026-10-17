# v7.1.9 operation and historical backfill

Healthy enrolled hosts with automatic installation enabled discover the signed
release and wait for all configured rooms to be empty together. Publication on
GitHub alone does not deliver an automatic update; the signed feed is the
authority. Leave normal gameplay running. Do not close occupied rooms, reinstall,
reauthorize, discard history or repeat ownership recovery to obtain this update.
Existing update pauses, opt-outs and stopped-service choices remain in force.

The main kit preserves v7.1.8 maintenance and recovery procedures. Older pinned
repair launchers and installer assets retain their own versions. Use this kit's
launcher for a new repair only when a local supported repair is actually needed.
For automatic delivery, no repair command is necessary.

Consuming the hub's shared historical statistics needs no local history import.
Once the source host has completed its reviewed upload, community clients fetch
requested players through the normal cache agent. A cold cache briefly waits
for a background lookup; a confirmed absent/hidden name gives the same generic
unavailable reply. Negative lookups expire after two minutes. No command performs
a blocking hub or osu! request.

## Reviewing a source host's own historical data

This section applies only to the host supplying old history, not to clients
consuming another host's records. A sharing default is not proof that old rooms
were public. Review room IDs against the operator's own records and exclude all
private or unknown rooms. Do not publish the reviewed file or private ledgers.

`install_history_review.py` is included in the checksum-verified kit. It accepts
this exact JSON on standard input and binds it to the currently installed
v7.1.9 runtime and installation:

```json
{
  "format": 1,
  "installation_id": "YOUR_INSTALLATION_UUID",
  "generation": "YOUR_GENERATION_UUID",
  "public_room_ids": [123456],
  "public_history_confirmed": true,
  "include_lobby_totals": true,
  "exclude_unreviewed_players": true
}
```

The default source is the current native ledger. To use the fixed preserved
pre-v7.1 migration backup, add `"source": "migration-backup"` and
`"source_sha256": "YOUR_VERIFIED_BACKUP_SHA256"`. No arbitrary source path is
accepted. The backup must be private, checkpointed and unchanged. Current native
hidden-player choices are checked even when importing from the older backup.
Do not expose the backup or credentials to another operator.

After independently enabling statistics sharing, installing v7.1.9 and finishing
any pending deployment, install the reviewed manifest:

```sh
sudo /usr/bin/python3.12 -I -S -B ./install_history_review.py --instance INSTANCE < reviewed-history.json
```

The installer verifies the exact protected runtime, source checksum, sharing
choice and installation binding. It refuses while another deployment is pending
so its configuration proof cannot be invalidated. It installs only the manifest;
it does not start services, alter sharing choices or import synchronously. The
already running network reporting agent captures small chunks and uses ordinary
authenticated uploads. No room restart is required for this configuration.

`state/activity-history-progress.json` records capture progress. `queued` means
captured into the local durable outbox, not accepted by the hub. Check reporting
support and central ingestion to confirm delivery. A large historical backlog
can take substantially longer than a normal cached player lookup. Preserve
state on refusal; do not delete consent, history progress or outboxes to retry.
Changing a previously bound review or consent requires a fresh maintainer review.

All eligible reviewed historical PP is retained. Cumulative time is withheld for
players with any recorded unreviewed/private-room participation or no recorded
room provenance, because scalar totals cannot be split reliably. Historical
mode detail unavailable in the source is not invented. Live activity continues
to use positively public interval evidence and existing privacy rules.
