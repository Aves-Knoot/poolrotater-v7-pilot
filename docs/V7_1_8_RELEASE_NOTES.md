# v7.1.8 maintenance, recovery and operator administration

This controller release adds supported maintenance announcements and draining to
the existing running controller. Operators can request an empty-room drain or an
explicit one-map warning, inspect progress, and cancel by operation ID. A confirmed
warning and real match boundary precede any occupied one-map closure. Empty idle
rooms close through their independently checked empty-room path. Late joins
postpone closure; ordinary automatic updates never choose occupied maintenance.

Warning refusals keep gameplay running. Unknown warning delivery is not replayed.
Uncertain closes require review instead of another close attempt. Cancellation is
rechecked at the final roster lock. Already closed rooms are never silently reopened.

Provider process startup validates its working directory before admission and
sends large private arguments through deadline-bound IPC after a small bootstrap.
Failed children are cleaned up with pinned process identities. Positively
undispatched lock contention is retryable; invalid authority, uncertain dispatched
mutations and corrupt recovery remain strict failures.

Consecutive stopped controller upgrades can retire positively closed launch hints
only through exact committed immutable history, including approved older workers.
Closed parked aliases are accepted without rewriting ownership evidence. Archived
receipt directories retain service-account readability under restrictive umasks.
The updater can reuse a recognized stopped native timer launcher across releases;
custom or active services are refused.

Status/support show installed and running controller versions, individual worker
versions and ownership, queued targets/blockers, maintenance progress and concrete
next actions. Lifetime counters and stale observations remain distinct from current
faults. No credential or arbitrary remote-command surface is added.

All existing features, tuning, settings, OAuth/Discord configuration, full catalogs,
enrollment, ratings, player history and explicit sharing/update/enablement choices
are retained. No storage migration or dependency change is required. The v7.1.7
live startup/refresh timeout fix and v7.1.6 prompt feedback repair remain included.

Install the current ZIP asset and read MAINTENANCE_OPERATOR_GUIDE.md. Existing
opted-in running hosts update naturally only when all configured rooms are safely
empty. Compatible worker-only releases still update each room independently.
Stopped or recovery-required hosts need their local supported recovery/start flow;
publication cannot remotely start them. Older controllers must first install this
release before using its new maintenance commands.

Validation includes frozen provider/administration/native regressions, real worker
RPC, full-catalog gameplay/maintenance fixtures, isolated root/service-UID direct
and consecutive upgrades, explicit opt-out/history preservation and timer guards.
Unchanged whole-repository and donor suites are not repeated. Actual production
room interruption is excluded from qualification. The older community refresh
archive proves a successful replacement creation followed by failed confirmation
and service teardown. Local cooldown explains a reproduced failure path, but the
original raw request ledger was absent, so its exact refusal remains unproven.
