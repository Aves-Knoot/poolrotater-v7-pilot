# v7.2.9 — managed rooms, private lobbies and fleet administration

A shared administration model now covers owner and community rooms. Authorized
staff can inspect defaults and effective settings, change live-safe settings,
and submit durable open, close, restart and configuration requests. Requests
carry installation/generation/revision bindings, scoped authorization and clear
completion receipts. Configuration that needs recreation waits for a safe
boundary and affects its target room; occupied siblings keep playing.

Room running/stopped intent survives updates, recovery and reboot. Actual
upstream closure is verified; leaving the referee is insufficient. Independent
administration remains available with all workers stopped or a failed
controller. A fixed protected helper can start an admitted controller; there is
no remote shell. Ordinary automatic maintenance never revives intentionally
stopped rooms.

The hosting operator controls a persistent concurrent-room ceiling, default4,
shared by public/private rooms and pending/uncertain creations. The provider's
allowance also applies. Reservations are atomic; central room commands cannot
raise or bypass the ceiling. Lowering the ceiling, including to0, blocks new
creation and lets existing rooms drain without abruptly ending matches.

Managed participation includes shared ratings and player history/statistics,
staff-only chat/diagnostic reporting, signed automatic updates and central room
administration. These have no separate in-product opt-outs. Local credentials
remain local; public website/Discord projections exclude private rooms, their
chat, participants and passwords. Old hosts retain legacy operation until their
capabilities prove migration; unsupported administration is reported explicitly.

Existing native gameplay, room defaults, player commands, OAuth, Discord
configuration, full catalogs, ratings/history and integration identities are
preserved. Discord labels read the running room type and panels omit map
progress; the website retains playback progress. Intentionally stopped rooms
are excluded from false outage alarms. Historical logs remain retained with
older-log compression.

The publisher can mirror the complete owner map-exclusion policy into signed
compatible feeds automatically. Only map IDs leave private moderation records;
unchanged policy does not cause needless publication. Removing a local entry
never silently reverses an already signed exclusion.

Upgrade admission and configuration preparation wait boundedly for confirmed
local process or owner-lock cleanup. Unknown state still refuses; an admitted
upgrade transaction is never blindly replayed. This repairs transient-reader
holds found by the real installed automatic-updater qualification.

This is a controller update. Exact published v7.2.6/v7.2.7/v7.2.8 updater paths
must pass installed acceptance before publication. Each host uses its verified
host-wide empty/idle gate, with no forced occupied-room interruption. Queueing,
waiting-empty, installed and verified healthy are separate deployment states.

Validation:316 final candidate cases and83 standalone feature cases passed.
The full retained reference covers268 modules/3604 reported cases, with eight
ordinary skips explicitly recorded; both root-only method bodies separately
passed on the donor. Relevant follow-up regression groups passed after localized
fixes; passing unchanged full suites were not repeatedly rerun. See VALIDATION.json.

Installed qualification used actual protected systemd services and all five full
catalogs. Native lifecycle/private target recreation, controller failure/replay,
real donor reboot and mixed hub checks use the retained revision13 reference.
The four-room original26 updater path uses revision15. Reference27→18 completed
after a documented QA resource correction and same-operation resubmission. The
separate exact28→final19 original updater path passed without resubmission. Initial
failed attempts are retained. Real final migration cleanup guards were tested
with dedicated-UID readers and owner locks; configuration drift still refused.
Final19 additionally waits before upgrade admission, refusing persistent holders
within20 seconds; it never replays an admitted transaction.

The actual sealed signer service and five-minute timer published the complete
invented owner list and its next addition with all full catalogs unchanged;
private moderation notes stayed private. Relevant signer code is identical in
the final runtime. The final runtime and installer rebuilds are byte-identical.

No actual osu! gameplay was performed for this qualification. Production update
queues and installed versions are separate from publication; occupied hosts
wait for the proven safe gate. Credentials and private moderation evidence are
excluded from these artifacts.
