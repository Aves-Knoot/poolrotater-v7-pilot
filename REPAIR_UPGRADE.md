# Repair an existing dev1 pilot installation

Use the replacement installer archive supplied by the maintainer. This procedure
keeps the same instance, dedicated UID, account/client configuration, authorization,
catalogs and ownership journal. It does not start a service, authorize again,
close/adopt a room, or clear uncertain ownership during the upgrade.

Keep the existing service stopped. Do not delete its journal or install another
instance to bypass it. Only the dedicated pilot unit is addressed here.

## 1. Upgrade protected code

Verify the replacement archive against the maintainer's independently supplied
archive digest, then extract it into a new directory. Set these variables in the
same shell. The replacement runtime digest must match the maintainer's release
record; the archive digest and runtime digest are different.

```bash
kit_directory=/absolute/path/to/extracted/replacement
old_runtime_sha=5637ca212115ee50bfa4f1b9d6d287dca9a3bf7785ec9b1c771459016a7cda8d
new_runtime_sha=REPLACE_WITH_MAINTAINER_RUNTIME_SHA256
sudo /usr/bin/python3.12 -I -S "$kit_directory/install.py" \
  --instance first --bundle "$kit_directory/runtime.zip" \
  --sha256 "$new_runtime_sha" --live-pilot \
  --upgrade-from-sha256 "$old_runtime_sha"
```

The old digest above identifies the published dev1 runtime. If your installed
digest differs, stop and ask the maintainer. Do not substitute a guessed digest.
Use your existing instance name instead of `first` if applicable. No preset or
account change belongs in this transaction.

Success reports `upgraded` (or `already_upgraded` for an exact completed repeat),
`service_started: false` and the new registered `release` path. Copy that path:

```bash
pilot_release=/exact/release/path/from/successful/upgrade
sudo /usr/bin/python3.12 -I -S "$pilot_release/poolrotater_host/pilot_admin_entry.py" \
  --instance first status
```

The repair upgrade may proceed with an uncertain room journal, but preserves that
uncertainty. Startup remains blocked until a separate recovery succeeds. The
dedicated unit retains graceful parent shutdown and gains a final cgroup kill
fallback after its 90-second stop timeout. Killing a process does not prove that
its upstream room closed.

If upgrade execution is interrupted, keep the service stopped and inspect the
durable transaction using the same pinned installer:

```bash
sudo /usr/bin/python3.12 -I -S "$kit_directory/install.py" \
  --instance first --bundle "$kit_directory/runtime.zip" \
  --sha256 "$new_runtime_sha" --live-pilot \
  --upgrade-from-sha256 "$old_runtime_sha" --upgrade-status
```

With its exact `transition_id`, explicitly resume using `--upgrade-recover complete
--transaction-id ID`, or abort using `--upgrade-recover abort --transaction-id ID`.
Aborting is allowed while the transition remains prepared; a committed decision
cannot be reversed through resume. A prepared upgrade requires explicit recovery;
an ordinary retry never guesses which code/bootstrap should become active. If no
transaction exists, or files/bindings differ, stop and return the redacted error.
Do not edit the protected records or bootstrap.

## 2. Recover the failed room creation

These commands use your existing local authorization for fresh authenticated
identity/client checks and read-only room API requests. They never send referee
room mutations. No credentials or authorization codes need to be shared.

```bash
sudo /usr/bin/python3.12 -I -S "$pilot_release/poolrotater_host/pilot_admin_entry.py" \
  --instance first ownership-review
```

For a lost initial creation, the review remains `unknown_creation`. Only explicitly
associate a room that you have confirmed was created by that failed attempt.
The maintainer cannot infer this from its title or a filtered listing. If there
are multiple possible rooms, or you cannot identify the created room, stop.

Copy the review's exact `journal_sha256` and the confirmed numeric room ID:

```bash
journal_sha=REPLACE_WITH_REVIEW_JOURNAL_SHA256
created_room_id=REPLACE_WITH_CONFIRMED_ROOM_ID
sudo /usr/bin/python3.12 -I -S "$pilot_release/poolrotater_host/pilot_admin_entry.py" \
  --instance first ownership-clear --journal-sha256 "$journal_sha" \
  --room-alias room1 --created-room-id "$created_room_id" --confirm-lost-creation
```

This operator assertion associates the candidate with the lost attempt; it is
recorded locally with the original journal. Independent fresh API evidence must
confirm the expected account owns that room and that it is inactive with a past,
timezone-qualified end timestamp. Active, foreign, missing, scheduled or conflicting
evidence refuses recovery. Other unresolved rooms also block recovery. A 404,
listing omission, public filter or elapsed time never proves closure.

`ownership_recovery_unresolved` means recovery did not qualify; retain the state
and wait for or investigate confirmed ended evidence. Rate-limit/outage refusals
also preserve state. Do not repeatedly start or recreate rooms.

Successful recovery reports `recovery_applied: true`, `phase: committed` and
`start_allowed: false`. Normal startup independently repeats its admission checks.
If recovery was interrupted, use its local transaction ID and explicitly complete
or abort. To read only that receipt for instance `first`:

```bash
sudo /usr/bin/python3.12 -I -S -c 'import json; v=json.load(open("/var/lib/poolrotater-v7-pilot/first/state/ownership-recovery.json")); print(json.dumps({k:v[k] for k in ("transaction_id","phase")}))'
sudo /usr/bin/python3.12 -I -S "$pilot_release/poolrotater_host/pilot_admin_entry.py" \
  --instance first ownership-resume --transaction-id REPLACE_WITH_TRANSACTION_UUID \
  --decision complete
```

Completing obtains fresh ended evidence again. `--decision abort` restores the
original uncertain journal; it does not resolve the room or permit startup.
Do not send the full private receipt, profile, token files or journal to others.

## 3. Separate supervised retry

After a committed upgrade and recovery, return redacted `support` and the recovery
result to the maintainer. Agree on a one-room observation window and follow the
existing `ONE_ROOM_CHECKLIST.md` for start, finding the room, shutdown and reporting.
No retry starts automatically as part of this repair. Actual account/room behavior
still requires this separate operator test.
