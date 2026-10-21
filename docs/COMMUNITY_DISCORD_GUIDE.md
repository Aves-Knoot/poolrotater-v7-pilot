# v7.2.9 — owner and community Discord panels and administration

Owner slots1–4 remain reserved. Community slots5+ stay bound to the enrolled
host and room alias across reconnections and room refreshes. Labels describe
the running room type and include the community operator. Adding text channels
named lobby-13-community, lobby-14-community and so on extends panel capacity;
existing assignments retain channel IDs after renaming. Slots are never stolen
from another room or silently recycled while its host is offline.

Panels retain player counts, room settings, difficulty, current map and verified
artwork. Elapsed/remaining map progress is omitted from Discord panels; website
playback progress remains. Delayed or unavailable observations do not authorize
room mutations. Private rooms, their links, players, chats and passwords are
excluded from public panels and the public directory.

Ordinary MOD/ADMIN match and player-command controls continue through the scoped
room relay. They carry installation/generation, room identity and expiry checks.
An edge reaction means queued, not confirmed execution. Uncertain ordinary chat
commands are not blindly replayed. This route grants no arbitrary shell or
account access. Discord credentials remain central; osu! OAuth stays on its host.

A separate verified private staff channel exposes .fleet controls for room
open/close/restart, persistent defaults, settings inspection and durable receipts.
See MANAGED_HOST_ADMIN.md for roles, safe boundaries, immediate-action semantics
and compare-and-set selectors. Its ID is fleet_channel_id in the central bridge
configuration and cannot overlap a lobby panel. Both incoming and outgoing
messages require staff-only visibility. Individual viewing grants are refused
except for the guild owner and bot; use staff-role channel permissions. Existing role checks and channel IDs
remain intact. Intentionally stopped rooms do not become outage alerts.

Managed participation includes relay/admin connectivity, shared ratings and
statistics, private staff diagnostics/chat reporting and signed automatic updates.
There is no per-feature opt-out in managed settings. The hosting operator retains
a persistent concurrent-room ceiling (default4, including public/private and
pending or uncertain creations), further bounded by the provider allowance.

This controller release uses a verified host-wide empty/idle update gate. An
occupied room is not forced down. Settings, OAuth, enrollment, catalogs and
history are retained, and stopped intent survives update/recovery/reboot. Old
hosts remain operational while upgrading and report unsupported capabilities;
a missing old-hub endpoint is reported as hub-upgrade-required. A shared catalog
or bridge outage never grants room-ownership authority.

See VALIDATION.json for exact synthetic installed acceptance and limits. No real
player command or production provider mutation is injected as an acceptance test.
