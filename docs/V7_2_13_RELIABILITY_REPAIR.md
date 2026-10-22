# v7.2.13 — community chat, reporting and Discord repair

A definitive HTTP 403 while sending a lobby announcement previously escaped the
worker loop and closed a room. Chat-only send/read/ACK denials now defer or omit
the affected chat work while gameplay continues. Rejected messages are not
reported as delivered, and ambiguous sends are never replayed. Authentication,
room ownership and initial channel-access checks remain strict.

The installed live controller now starts and stops the same Discord command
relay as the native supervisor. Community panels and the command relay remain
separate: a visible panel alone does not prove commands are connected.

Hub player-statistics snapshots now reuse independently verified on-disk v1/v2
results. Public API handlers do not rebuild the full player database or wait for
another snapshot build. One bounded background builder refreshes immutable
snapshots; same-policy prior data may be used during refresh, but privacy or
authority changes immediately withhold it. Only one full in-memory projection
is retained. Snapshot collection releases the hub authority transaction before
its large scan, then rechecks authority before publication. Ordinary host-to-hub
requests have a 30-second deadline instead of five seconds.

An admitted controller switch temporarily pauses only its own managed, update
and recovery timers. It restores their original active states, preserving the
native owner lease and process-census checks. This avoids independent helper
checks racing stopped preparation. Occupied rooms still prevent admission.

This release supports the exact v7.2.12 predecessor. Older managed hosts first
use their existing supported path to v7.2.12. Configuration, OAuth, hub identity,
ratings/history, full catalogs, room ceilings and deliberate stopped slots are
preserved. No reinstall or fresh authorization is required. The hub repair can
be deployed independently; controller changes use the normal safe update gate.
A recovered stopped host may resume its previously saved running intent.
