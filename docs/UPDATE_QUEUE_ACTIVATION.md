# First managed-host update activation — v7.2.12

v7.2.9 and v7.2.10 can refuse the first subsequent controller update while all
rooms remain running. Their root queue reads the service-owned managed-settings
projection with the wrong effective UID. v7.2.12 keeps the strict ownership and
privacy checks and reads that file under the existing service identity.

An affected host needs the pinned `poolrotater-activate-v7.2.12-v2.py` release asset
once, run as root with `--instance YOUR_EXISTING_INSTANCE`. Verify its checksum
against `RELEASE_SHA256SUMS.txt` from the matching GitHub release first. The
activation downloads the exact pinned runtime, validates its archive, and uses
that release's corrected native queue code with the host's existing trust.

The activation queues only. It does not stop or start rooms, force a restart,
change OAuth, replace enrollment, clear ownership, reinstall the host, or alter
saved settings. Keep the normal updater running; it applies the queued
controller only at its verified safe empty-room boundary. Deliberately stopped
slots remain stopped. Future ordinary updates use the repaired installed code.

If the earlier preflight left a held scheduler operation without a matching
native job, the bridge can retire only that unsubmitted intent after checking
the unchanged baseline and an older terminal native job. Its evidence/history
is retained. An uncertain or active different native update is refused; it is
never erased or cancelled to make admission pass.

Return the final JSON and redacted update status. `waiting_empty` means queued,
not installed. Do not retry startup after a refusal or uncertain outcome; retain
state and return the failure code. A host already on the exact runtime reports
`already_installed` without creating another operation. Older releases retain
their normal signed update path and do not need this special activation.

The v7.2.12 selector also prefers a signed direct predecessor before walking
intermediate releases. This prevents an older optional predecessor from being
selected solely because its hash sorts first. Required compatibility hops,
signatures, anti-rollback state and exact installed-baseline checks remain.
Use the v7.2.12 activator; the earlier v7.2.11 activator can safely refuse with
`queue_activation_signed_target_changed` without queueing or stopping rooms.
