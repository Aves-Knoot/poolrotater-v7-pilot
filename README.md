# PoolRotater distributed host

R25 v7.3.3 has passed the two normal installed automatic-upgrade paths from
v7.3.0 and v7.3.2. One additional pre-admission controller-interruption case
failed before the candidate could be installed; it remains an explicit legacy
limitation and is not claimed as recovered. Install only matching GitHub release assets or the signed automatic update. See [release details and qualification](docs/V7_3_3_TAIKO_RATINGS.md).
Use the installer ZIP asset of an accepted GitHub release; source archives do
not install a protected runtime.

Owner and community hosts share native map selection, ratings/statistics,
reporting and authenticated administration. See [managed room administration](docs/MANAGED_HOST_ADMIN.md),
[upgrade notes](docs/V7_3_1_CONFIGURATION_UPGRADES.md), and the installer kit guides.
See [controller failure diagnostics](docs/CONTROLLER_FAILURE_DIAGNOSTICS.md) for
the new evidence capture and the recurring crash investigation's limits.
Existing configuration, account authorization, catalogs and player history are
retained. Private-room information never belongs in public directory panels.

Affected v7.2.9/v7.2.10 hosts: see [first-update activation](docs/UPDATE_QUEUE_ACTIVATION.md)
for the pinned queue-only bridge. No reinstall or repeated OAuth is needed.

Existing v7.2.12 hosts receive the signed controller update at the safe host-wide empty-room boundary. The central hub can be repaired independently.

Accepted builds are distributed through GitHub and the signed release feed.
Use the matching release's operator kit for the
per-room empty drain, state-preserving upgrade and conditional startup workflow.
