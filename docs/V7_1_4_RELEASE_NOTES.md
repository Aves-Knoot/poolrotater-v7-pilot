# v7.1.4 — full host diagnostics

Expand default reporting to include emitted native DEBUG logs, private-room
diagnostics/chat, early worker stdout/stderr and the instance's bot/network/updater
systemd journals. Credentials are scrubbed before upload queue persistence and
again at the hub; private diagnostics stay off the public website.

Keep explicit operator reporting choices. The historical `public_chat` switch
controls chat/commands across all room visibility; `operational_logs` controls
all diagnostics. Statistics and directory privacy are unchanged.

Preserve unread numbered rotations, split large messages only after redaction,
refresh cached credential values immediately on metadata/environment changes,
commit journal records and cursors atomically, and resume safely after journal
vacuum/reboot without duplicating acknowledged overlap. Collection remains
bounded, compressed and independent of gameplay/provider requests. Retention and
storage loss counters remain visible; see the operator guide for exact limits.

Includes v7.1.3 room-worker responsiveness. This is a shared-controller release,
so activation uses the existing all-configured-rooms-empty window. Existing
settings, credentials, catalogs, histories, enrollment and explicit opt-outs stay.
Publication does not start a stopped operator host.
