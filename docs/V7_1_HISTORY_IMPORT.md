# Reviewed v7.1 public-history import

This is a separate, optional operator action after an exact qualified v7.1.0
installation or upgrade. It is not performed by installation, enrollment, the hub
transition, or normal post-opt-in collection. No production import has been run as
part of this development qualification. Existing local history remains available
locally even if an operator declines sharing or cannot establish public provenance.

Use the checksum-verified release kit's `import_v71_public_history.py` helper, with
system Python as root. The helper accepts only an existing protected installation
of the sealed runtime
`8c0971747c77a22a992749a6171363d60eb4a3bdc5f87460c55f88d881a21d35`.
It verifies the root-owned administrative bootstrap, entire registered source and
dependency environment, installation/generation binding and service identity. It
then drops to that installation's actual service UID before reading native player
history. No OAuth/Discord settings are requested, no provider request is made,
and no service is stopped or started by this helper.

## Stop and review first

Arrange an empty-room maintenance window; do not interrupt an occupied room just
to import history. Cleanly stop the bot with the protected administration entry,
then stop its network agent and any installed update service/timer. Keep all units
stopped during each local import invocation. The helper checks these exact names
for the chosen instance:

- `poolrotater-v7-pilot-INSTANCE.service`
- `poolrotater-v7-network-INSTANCE.service`
- `poolrotater-v7-updates-INSTANCE.service` and `.timer`
- `poolrotater-v7-controller-update-INSTANCE.service` and `.timer`

Absent auxiliary units are permitted. Loaded units must be inactive or failed
with service MainPID 0 (timers do not normally expose MainPID), no dedicated-UID
processes may remain, and the ownership journal
must be cleanly stopped/parked. An active update timer is a refusal even if its
service currently has no process. Retain state and resolve an ownership recovery
requirement through the normal documented flow; do not clear or edit the journal.

Set these variables to the existing instance and extracted verified kit. The
release path below is fixed to this qualified release, not an arbitrary source
checkout. The command examples are intended for an administrator's shell:

```bash
umask 077
pilot_instance=two
pilot_kit=/absolute/path/to/verified/kit
pilot_release=/opt/poolrotater-v7-pilot/$pilot_instance/releases/v7.1.0-8c0971747c77a22a
sudo /usr/bin/python3.12 -I -S -B "$pilot_release/poolrotater_host/pilot_admin_entry.py" --instance "$pilot_instance" network-reporting-support
sudo nice -n 15 /usr/bin/python3.12 -I -S -B "$pilot_kit/import_v71_public_history.py" --instance "$pilot_instance" > history-plan.json
```

`history-plan.json` contains the installation ID, generation and source room IDs
for independent review. Keep it private. Room IDs are candidates for review,
**not evidence that those rooms were public**. Names, current password state,
absence from today's room listing, successful past gameplay, and ownership do
not establish historical visibility. Consult your actual room history and private
test records. Do not copy the entire suggested list into an approval automatically.

All four new reporting choices default off and remain independent. A plan does
not enable them. If statistics sharing is still disabled, obtain explicit operator
agreement before enabling it. Consumption, operational logs and public chat each
need their own choice; enrollment or ratings contribution does not imply any of
these choices. For an operator who independently chooses statistics sharing only,
create the following private file and apply it through the protected command:

```json
{"share_statistics":true,"consume_statistics":false,"operational_logs":false,"public_chat":false}
```

```bash
sudo /usr/bin/python3.12 -I -S -B "$pilot_release/poolrotater_host/pilot_admin_entry.py" --instance "$pilot_instance" network-reporting-configure < reviewed-reporting-choices.json
```

If reporting is already configured, preserve its reviewed choices; do not blindly
replace them with this example. Import refuses disabled local statistics consent.
It does not grant or change hub consent itself; the independent network agent
later sends the normal authenticated consent and upload requests.

## Explicit public provenance manifest

Create a private `history-review.json` with exactly these fields. Replace the two
UUIDs with this installation's plan values and the numeric IDs with independently
confirmed historical public rooms:

```json
{
  "installation_id":"REPLACE_WITH_PLAN_INSTALLATION_UUID",
  "generation":"REPLACE_WITH_PLAN_GENERATION_UUID",
  "public_room_ids":[123456],
  "public_history_confirmed":true,
  "include_lobby_totals":false
}
```

The safe partial-history choice is `include_lobby_totals:false`. It imports only
existing tracked PP achievements whose actual room ID is in the explicit reviewed
list. Unknown/private room achievements are omitted. There is no new PP calculator,
profile-difference attribution or invented score. Reviewing additional public IDs
later rescans historical PP with source-key deduplication, preserving prior imports.

`include_lobby_totals:true` additionally publishes existing cumulative legacy
lobby-time summaries. Those old totals cannot be separated reliably by room. This
requires an independently established **all-public source history**, not just a
list of the rooms one wants to share. The reviewed manifest must cover all rooms
recorded in the native source, and no authoritative private-presence evidence may
contradict it. Consider historical private owner test rooms too. If any part of
cumulative history could include private/unknown room time, leave this false and
use reviewed PP plus new public activity after opt-in. Never approve a known
private room merely to get past the complete-source check. Removing private rows
or editing cumulative totals is not a supported workaround.

Cumulative baselines retain their actual persisted totals and last native
checkpoint. They do not manufacture elapsed time from stale open joins, distribute
legacy Mania totals into invented 4K/7K modes, or claim exact historical overlaps
with other hosts. New canonical captured intervals have separate precise coverage.

## Apply bounded chunks, then drain

```bash
sudo nice -n 15 /usr/bin/python3.12 -I -S -B "$pilot_kit/import_v71_public_history.py" --instance "$pilot_instance" --apply --batches 20 < history-review.json > history-import.json
```

The helper imports at most 20 bounded scans by default; `--batches` accepts 1–100.
Each underlying scan considers at most 500 rows per achievement table and 500
staged time baselines. A zero newly-queued count is not itself proof of completion:
private, hidden or duplicate rows may precede later reviewed rows. Read the
explicit `history_scan_complete` and `import_may_need_more_batches` fields.

- If more scans are needed without a capacity pause, repeat the same command with
  the same reviewed manifest while units remain stopped.
- If `history_capture_paused:true`, retain all state. The ordinary outbox is bounded
  at 50,000 events; resumable baseline staging supports sources larger than this
  queue. Drain through the independent network agent before another stopped
  import invocation. Do not delete queues or reset consent to create space.
- If `history_scan_complete:true`, the reviewed scan is locally complete. This is
  not proof that the hub accepted it or that another host has refreshed its cache.

The native ledger is never rewritten. A private bound review receipt is kept at
`state/activity-history-review.json`; cumulative source baselines are frozen per
consent epoch, and normal retries/restarts do not increment or duplicate them.
Keep the same consent and state while completing one import. Changing the source,
reenabling consent, or broadening a cumulative review deserves a fresh operator
review rather than an assumption that previous permission automatically applies.

With the local invocation finished, leave the bot stopped and resume only the
network agent (or run bounded manual synchronization) to send consent and queued
events. Manual synchronization uses the normal enrolled host authentication and
may need repetition; it does not start rooms:

```bash
sudo /usr/bin/python3.12 -I -S -B "$pilot_release/poolrotater_host/pilot_admin_entry.py" --instance "$pilot_instance" network-reporting-sync
sudo /usr/bin/python3.12 -I -S -B "$pilot_release/poolrotater_host/pilot_admin_entry.py" --instance "$pilot_instance" network-reporting-support
```

For sustained draining, use the existing protected `network-start`, then stop that
agent again before another local helper invocation. Restore only previously
intended update services/timers after the final import. Starting rooms is a
separate operator decision after normal ownership/startup checks.

Hidden local users are excluded from historical import. Normal upload first
refreshes privacy and purges newly hidden pending records; participating-host hide
preferences also suppress shared named results globally. Another host cannot undo
a hide. Use `.show` at the originating host(s) when that user wants to remove
those preferences. Do not send raw native databases, review receipts, player rows,
OAuth files or private instance hashes to support. Redacted queue counts, explicit
scan-completion flags and synchronization results are sufficient initially.

## Qualification boundary

Helper unit checks use invented isolated ledgers and manifests. They cover
installation binding, incomplete all-source refusal, preserved native totals,
source-key replay, private-first chunks, expanded review, capacity drain/resume,
active timer refusal and clean ownership/lease requirements. The actual protected root/service-UID launcher also passed donor qualification
using the exact sealed runtime and helper below. The large
700,000-user source used for scale testing is private **synthetic test data**, not
production history or permission to import real owner history. No real operator
history has been automatically approved, uploaded or made public by these tests.


The completed donor receipt is
`validation/v7.1.0-donor-final2/history-helper-report.json`. It pins helper SHA-256
`2312e6ed15e44575adb44efcfc405699f60298d3de115378c53464c9b71aedd3`
and runtime SHA-256
`8c0971747c77a22a992749a6171363d60eb4a3bdc5f87460c55f88d881a21d35`.
The real root bootstrap verified the protected registered source/environment,
checked stopped units and UID-process absence, and dropped to the actual service
UID for native SQL access. An installation-bound reviewed manifest queued three
invented historical events, including a tracked PP achievement; the repeated
import queued zero. Both scans reported complete. No services were started, no
provider request was dispatched, and no production data was changed.
