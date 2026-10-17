# v7.1.10 operator guide

Keep the existing installation,
configuration and state. Do not reinstall, reauthorize, repeat ownership-clear or
change sharing/update choices merely to receive this update.

## Healthy hosts with automatic updates

The existing signed updater discovers an approved successor according to its
saved polling policy. Publication does not immediately queue or start a host.
Each host waits for all of its configured rooms to empty naturally, then performs
the guarded controller transition. Intentionally disabled rooms are not part of
that host's empty-room barrier. Occupied rooms must not be closed to accelerate it.

The predecessor runs the initial migration until registration switches. New
recovery behavior is not retroactively added to immutable old releases merely
because a new release was published. Keep the stopped-state recovery procedure
available during the first transition; do not retry an uncertain outcome blindly.

## Understanding a held or interrupted update

Use existing `status`, `support`, `update-status` and the retained controller
operation ID. The queued controller view includes safe `failure_stage` and
`failure_code` values when present, and distinguishes services unexpectedly
offline from an ordinary empty-room wait. Preserve the transaction and reports.
A failed automatic update that leaves expected rooms down is an outage.

The bounded cleanup wait covers only a busy native owner lock or surviving
processes belonging to the dedicated service UID. Persistent cleanup, unknown
ownership, changed configuration/artifacts and uncertain startup are retained
for review. Never delete owner locks, kill unrelated processes, clear unknown
ownership or edit journals to satisfy a guard.

Automatic recovery has at most three continuation attempts within a 30-minute
eligibility window. The existing supported operation-ID-bound review/completion
commands remain the explicit recovery route after a hold. An already healthy
successor is observed without restarting it. Publishing a release never grants
authority to start a separately stopped host.

## Stopped or held hosts: retain state and review the exact operation

Do not reinstall or delete a failed transaction. First capture `status`, `support`
and `update-status` using the existing protected operator entry. Preserve the
operation ID and failure stage/code. A read-only recovery review does not grant
permission to retry uncertain room creation.

For a v7.1.10 candidate already staged by the native controller updater, the
maintainer can use that candidate's protected entry even when the registered
predecessor is still older. Set `candidate_release` to the exact `after.release`
in the root-owned controller queue, and use that queue's operation ID:

```sh
sudo /usr/bin/python3.12 -I -S -B \
  "$candidate_release/poolrotater_host/controller_update_entry.py" \
  "$instance" --operation-id "$operation_id" --decision review
```

This entry verifies the complete staged code inventory, binding and runtime
before importing it. It refuses a source checkout, extracted installer or a
candidate belonging to another queue. Review the result and ownership evidence
before choosing an explicit `complete`, `cancel` or `rollback` decision through
the same entry. Those decisions have different guards; `rollback` only aborts a
verified unfinished registration transition, and never reverses a committed one.
An unknown or uncertain outcome remains a review hold.

Completion against a still-running predecessor restores saved reporting-service
intent, releases the old admission gate and requires a new empty-room window.
It does not restart that controller. A recognized controller timer is rearmed;
independent updater jobs continue through their existing scheduler. An already
running successor is observed without another start. Originally disabled network
sharing remains disabled.

The release's pinned `repair_and_start.py`/launcher is a separate stopped-instance
repair route, retaining its installation checks and one explicit start attempt.
Do not substitute it for review of an unrelated pending controller transaction,
or rerun it merely because the bot is intentionally stopped. Keep older launchers
with their original checksums; their published pins are immutable.

## Shared statistics and privacy

Use `network-reporting-support` for capture, pending events, privacy review and
reporting freshness. `identity_exclusions_last_scan` contains bounded counts,
not player IDs. Internal identities stay in the native ledger and never travel
as provider player IDs. A malformed real player's preference is a privacy hold;
correct the authoritative preference through a supported local operation.

An existing reviewed historical manifest resumes through the normal independent
reporting agent. This release does not implicitly approve private rooms or a
new historical source. Current hidden choices and sharing opt-outs still apply.

Distinguish these stages:

1. The reviewed source was captured locally.
2. Durable events await upload or receipt.
3. The hub accepted the eligible events.
4. Another host refreshed its shared cache and can display the historical stats.
5. Website totals include eligible community activity without adding the owner's
   already-counted baseline a second time.

For a retained incident, return redacted support/update/reporting output, the
operation ID, safe failure stage/code, versions and times. Do not send credentials,
token files or unredacted private state. Keep the native state for review.
