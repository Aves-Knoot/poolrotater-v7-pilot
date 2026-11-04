# Update capacity diagnostics follow-up

The source now accepts two additional fixed update-report error codes:
`update_staging_capacity_wait` and
`update_stopped_host_requires_controller_release`. The wire protocol and
authentication remain unchanged. Arbitrary error strings and unknown codes
are still refused.

These codes were already emitted by the updater but missing from its reporting
schema. A capacity wait could therefore leave an older generic retry error in
the central report even while the local journal correctly explained the wait.
The sender validates its projection before transmission, and the receiver
also validates the report. Both sides require the additive change; updating
the hub alone does not make an older sender transmit the newly recognized code.

The central receiver correction is deployed. The host-side change is unreleased
source and must enter a separately qualified host artifact. The existing
v7.3.10 runtime ZIP, release tag and signed offer are unchanged. Do not claim
that a source commit updated a running host.

## Capacity versus room admission

A capacity wait happens before creating an update operation. It does not close
rooms or authorize reducing the disk reserve. For the exact v7.3.10 package,
the guard requires 5,652,578,066 available bytes, including the five-GiB reserve
and the package's staging allowance. Other packages can require different
allowances. Compare actual available bytes on both installation filesystems.

Do not remove registered releases, live databases, pending artifacts,
rollback/recovery evidence, or active logs to accelerate an update. Use a
supported cleanup/archive procedure after checking the actual files and their
references. Recheck the existing update operation after space is available.

The older stopped-host admission is separate: an inactive host with retained
running-room intent can return `update_host_not_running`, even after native
ownership recovery has committed. That does not authorize changing intent or
repeating room creation. Follow the qualified preserved-intent continuation
for the installed version.

## Verification and memory diagnosis

Eight isolated focused cases cover sender projection, hub persistence,
old error compatibility, authentication, replay/conflict behavior and refusal
of unknown text, extra fields, invalid protocol and invalid bounds. This is
not a new gameplay or automatic-update acceptance claim.

A prior OOM counter or a rising container-memory graph does not identify a
leak. Compare timestamped per-process RSS/PSS and anonymous memory with cgroup
anonymous/file-cache/kernel usage, service/container limits and OOM counters.
Preserve live rooms while collecting those observations. No memory-limit or
room-restart workaround is part of this reporting correction.
