# PoolRotater distributed host

Prepared operator build: **v7.3.1**, configuration-safe upgrades over the v7.3.0 foundation.
Use the installer ZIP asset of an accepted GitHub release; source archives do
not install a protected runtime.

Owner and community hosts share native map selection, ratings/statistics,
reporting and authenticated administration. See [managed room administration](docs/MANAGED_HOST_ADMIN.md),
[upgrade notes](docs/V7_3_1_CONFIGURATION_UPGRADES.md), and the installer kit guides.
Existing configuration, account authorization, catalogs and player history are
retained. Private-room information never belongs in public directory panels.

Affected v7.2.9/v7.2.10 hosts: see [first-update activation](docs/UPDATE_QUEUE_ACTIVATION.md)
for the pinned queue-only bridge. No reinstall or repeated OAuth is needed.

Existing v7.2.12 hosts receive the signed controller update at the safe host-wide empty-room boundary. The central hub can be repaired independently.

This prepared build is handed to operators privately. Public GitHub and signed
release feeds are not changed by its preparation. See the private kit for the
per-room empty drain, state-preserving upgrade and conditional startup workflow.
