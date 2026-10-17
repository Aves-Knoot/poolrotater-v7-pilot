# v7.1.5 — cooldown-safe room refresh and stopped-host repair

An empty room could close for its scheduled refresh, successfully create its
replacement, then fail the separate ownership GET while an earlier HTTP timeout
kept the local request gate in cooldown. The replacement ID was lost and the
service entered recovery. The new version preserves that ID before verification,
defers scheduled refresh while admission is unavailable, and waits for a bounded
read-admission window without blocking the shared referee routing lock.

Candidate events remain buffered and grant no room authority until fresh owner
and activity evidence is verified. Generation changes, disconnects, invalid
provenance and lost worker authority refuse adoption. The fix never replays
MakeRoom or clears a pacing cooldown. Read diagnostics retain numeric HTTP status
and safe room/channel IDs. Stopped recovery can use a saved candidate, with fresh
exact account ownership and ended-room checks. Historical unknown creations still
need their independently identified room ID.

The pinned one-command repair launcher upgrades an existing stopped instance,
preserves all configuration and state, performs supported recovery, and attempts
startup once when explicitly requested. It refuses running old installations or
an in-flight updater. It does not reinstall, request OAuth again, reset tuning,
enable services or change sharing/update consent. Existing healthy running rooms
are never stopped by this helper.

This is a bounded successor to published v7.1.4. The paused maintenance/admin
workflow is excluded. Direct stopped upgrade contracts cover the exact published
v7.1.1, v7.1.2, v7.1.3 and v7.1.4 artifacts. Storage and pinned dependencies are
unchanged. Read V7_1_5_REPAIR_AND_START.md for the operator procedure.

Actual upstream gameplay remains an operator acceptance step. Publication and
download alone do not install this release or start a stopped host. The repair is registered in the signed index so a manual installation is
recognized. Existing channel targets remain unchanged; no owner-host rollout
is queued.
