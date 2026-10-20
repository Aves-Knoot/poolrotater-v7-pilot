# v7.2.5 — room recovery and failure containment

A normal skip could encounter a stale roster, attempt JoinRoom and receive a
server rejection. That rejection escaped the worker's recovery handler. The
controller then escalated its inability to close the stale-roster room into a
full-service shutdown. This release repairs both paths.

- A rejected JoinRoom enters the existing bounded recovery path. The interrupted
  command is not replayed, and no membership is assumed from a rejection.
- A dead worker's known room can obtain a fresh ordered roster and close without
  interrupting occupied siblings. In-flight cleanup gets a fixed30-second window;
  genuine unknown creation/close outcomes still require reconciliation.
- Includes staged v7.2.4 request-budget hardening: pacing admission does not consume
  the subsequent network allowance. Fixed-category phase timings report slow
  startup/request/response-body work without logging credentials or response bodies.
- Preserves v7.2.2 keepalive/cooldown fixes and v7.2.3 display grace.

## Existing hosts

This is a controller release. Healthy opted-in hosts apply it when all configured
rooms on their own host are empty; one empty room is insufficient. An occupied
room or late join postpones admission. The signed feed retains the intermediate
v7.2.2 and v7.2.3 hops for older hosts. Direct stopped upgrade is qualified from
v7.2.3; the unpublished v7.2.4 transition is also explicitly declared.

Keep the existing instance and state. No reinstall, OAuth reset, room-default
reset, repeated enrollment or automatic ownership-clear. Preserve explicit sharing
and automatic-update opt-outs. A publication alone does not authorize restarting
a stopped operator installation; recovery/start uses its existing admission rules.

## Validation

317 unique targeted regression cases passed after fixture corrections. Full-catalog
protected upgrades passed for owner (four rooms) and community (two rooms).
See VALIDATION.json for the exact results and validation limits. Isolated TLS/RPC and real worker processes reproduce the old failure and
exercise a playing sibling. No real community-account room is used in these tests.
Full repository suites and real community gameplay are not claimed.

## Platform and limits

Existing Linux x86_64/glibc, CPython3.12/systemd support is retained. Dependencies
and storage schema are unchanged. ARM/other OS support is not newly qualified.
Permanent provider failure and genuinely uncertain ownership still fail closed.
The original early-morning connection interruption is not established by this fix.
