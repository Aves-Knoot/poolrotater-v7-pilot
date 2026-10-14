# Upgrade an existing pilot installation to the single-room release

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
old_runtime_sha=e4db2fbe600ccc14ad323ba2d6ded80840718744613ff5de876a9765890219ab
new_runtime_sha=b014b75a68fe9adb1612700f74e0038e3d689a7e71debde59518d41a469f10e1
sudo /usr/bin/python3.12 -I -S "$kit_directory/install.py" \
  --instance first --bundle "$kit_directory/runtime.zip" \
  --sha256 "$new_runtime_sha" --live-pilot \
  --upgrade-from-sha256 "$old_runtime_sha"
```

The old digest above identifies the published dev6 runtime. This is the expected
starting point for the existing tester whose recovery already committed and whose
fresh ownership-review passed. For an installation still on dev5, use
`d75450d2d2f06f131dcc9b84b0951be0c471d85c90f82ae440b1e0e113f06ec2`
only after confirming that installed version. For an installation still on dev2, use
`e4bc5bf8fe8d70e3b859cc92f5b126048f53e7b5bc73b31e2661a8d9cd664bad`
only after confirming that installed version. For an
installation still on published dev1, the independently recorded old runtime digest
is `5637ca212115ee50bfa4f1b9d6d287dca9a3bf7785ec9b1c771459016a7cda8d`.
Use that value only after confirming the installation is still dev1. If your
installed digest differs from these known versions, stop and ask the maintainer. Do not guess.
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

## 2. Check already committed recovery, or recover a failed creation

### 2A. Recovery already committed or journal clean — existing dev6 tester

If the existing recovery for room 4666258 already reported `phase: committed`,
do not run ownership-clear again. After the code upgrade, run `status`, `support`
and a fresh `ownership-review`. The expected local status is `room_phase: stopped`,
`ownership_recovery_phase: committed`, and `ownership_status_source:
journal_and_receipt`. A retained `last_service_room_phase: recovery_required` is
historical diagnostic data from the dead run, not current ownership authority.
For dev6's cleanly closed room 4676871, no additional ownership-clear is needed.
The service's old `recovery_required` observation may describe its failure rather
than the clean journal. New runs report service `failed` separately from ownership.
The fresh review should report room1 as `local_clean`; it intentionally retains
`start_allowed: false` because review itself never grants a startup permit.

```bash
sudo /usr/bin/python3.12 -I -S "$pilot_release/poolrotater_host/pilot_admin_entry.py" \
  --instance first support
sudo /usr/bin/python3.12 -I -S "$pilot_release/poolrotater_host/pilot_admin_entry.py" \
  --instance first ownership-review
```

If review refuses, return the redacted `failure_code` and `failure_stage` along
with `support`. Retain state and hold startup. Do not share token/profile/journal
files. The old catch-all hid prefixed AuthError codes; this repair reads the stable
allowlisted code and labels authority/review/recovery stages. The exact cause of
the external dev2 refusal remains unverified until this fresh result is available.

After a clean review, continue with SINGLE_ROOM_TEST.md. Sections 2B below apply
only to an unresolved older installation; the existing tester skips them.

### 2B. Unresolved recovery — older installations only

For recovery that has not already committed, use the following steps.

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

## 3. Systemd failure accounting and separate supervised retry

An existing `service_state: failed` with MainPID0 is allowed for stopped recovery.
`systemctl reset-failed` is not required to commit recovery and cannot clear an
ownership journal or recovery transaction. A fresh start normally resets the
failed state. If systemd specifically refuses it with `start-limit-hit`, reset only
the dedicated pilot unit with `sudo systemctl reset-failed
poolrotater-v7-pilot-first.service` after confirming its exact installed unit name.
Do not target the production bot unit. Resetting failure accounting never bypasses
the application startup guards.

The original MakeRoom fix accepts a documented RoomJoinedResponse without a
top-level user_id only when the authenticated caller is among its referees and
a separate fresh room API detail confirms the expected account owns that exact
room. Conflicting creator evidence, absent referee membership, changed connection
generation or failed ownership evidence still refuses, without replaying MakeRoom.
It also uses a pilot label instead of an offline room name. This confirms the
format case covered by invented contract tests; it does not claim actual gameplay
acceptance.


After a committed upgrade and recovery, return redacted `support` and the recovery
result to the maintainer. Agree on a one-room observation window and follow the
existing `ONE_ROOM_CHECKLIST.md` for start, finding the room, shutdown and reporting.
No retry starts automatically as part of this repair. Actual account/room behavior
still requires this separate operator test.
