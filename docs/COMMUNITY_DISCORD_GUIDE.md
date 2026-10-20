# v7.2.7 — central community Discord panels and controls

- Owner slots1–4 stay reserved. Their Discord channel labels describe the room
  type and automatic/fixed difficulty. This changes Discord labels, not osu!
  room titles, ranges, account credentials or player settings.
- Community slots5+ bind persistently to enrolled host ID and room alias.
  Labels include the operator and type, e.g. l5-xikeon-standard-autodiff.
  Refreshing a room or reconnecting does not move it. Offline hosts retain slots;
  duplicate operator labels cannot conflict. Slots are not silently recycled.
- Add text channels named lobby-13-community, lobby-14-community, etc. in the
  configured guild to extend capacity. Discovery runs each minute. Existing
  assignments use immutable channel IDs after renaming, not channel names.
  Excess rooms wait for capacity; the bridge never steals another room's channel.
- Community panels show occupancy, difficulty, state, current map, verified
  central artwork, settings and qualified estimated map progress. Delayed status
  removes controls and progress; remembered offline identities survive restarts.
- Discord MOD/ADMIN checks remain. Authenticated, expiring commands bind to the
  host installation/generation and exact current room. The separate host thread
  admits them into the existing native room command queue and returns replies.
  Discord credentials stay central; community OAuth stays on its own host.
- Commands are never automatically replayed after an uncertain delivery. The
  green reaction means queued at the Discord edge, not proof of execution.
  Unconfirmed/refused requests receive a notice. Ordinary moderation/settings/
  say/start/abort controls are supported; remote shell/account changes and
  whole-host open/reopen/shutdown actions are intentionally outside this route.
- Relay outages and local relay storage errors are isolated from gameplay.
  No provider calls are made by the relay thread. Existing network sharing,
  OAuth, catalogs, activity and feedback remain unchanged.

## Existing hosts

This is a compatible controller update from v7.2.6. It waits until all configured
local rooms are empty, then uses the established guarded restart. No forced
occupied-room restart, reinstall, repeated OAuth or enrollment is needed.
The hub can serve older hosts concurrently: panels work from their existing
heartbeat data; controls refuse clearly until that host runs the relay release.
If an older hub lacks the endpoint, the host backs off and rooms continue normally.

The central hub administrator installs one community Discord bridge with existing
guild role policy and binds its owner-host identity plus reserved channel IDs.
No operator needs the owner's Discord token. Relay defaults enabled for enrolled
hosts. To opt out, locally write private owned config/discord-relay.json containing
{"format":1,"enabled":false}; this does not stop rooms or network heartbeats.

For a new Loved host, follow LOVED_OPERATOR_GUIDE.md. Full owner-prepared catalogs
and v7.2.6 Loved/DT fixes remain. Installer platform is still Ubuntu24.04 x86_64,
Python3.12 and systemd. Installation alone enables/starts no room.

## Validation

See VALIDATION.json for exact executed tests and protected full-catalog upgrade/
real-native-worker qualification. Provider and Discord traffic in development is
invented/loopback only. Production channel/readback and installed relay capability
are checked separately; no real player command is injected as a test.
