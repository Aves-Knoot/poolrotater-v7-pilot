# v7.3.10 focused maintenance

A busy SQLite writer could interrupt a controller settings refresh and stop
healthy rooms. Managed observations now use read-only transactions, busy state
writes defer with bounded retries, and configuration refresh retains its last
valid settings. Unavailable visibility reads retain both last-good status
observations together. Schema, identity, privacy and ownership failures remain
explicit refusals.

Interrupted committed upgrades stop a returned dedicated network helper before
stopped configuration preparation. Completion dispatches through the protected
staged successor, so an older SDK cannot render a successor's configuration with
the predecessor's version stamp. Current ownership and immutable inventory guards
still run; this does not permit retrying an unknown room creation or closure.

## Automatic delivery and preserved state

Published v7.3.8 hosts use their existing signed updater. Keep the installation,
OAuth authorization, hub enrollment, full catalogs, ratings/history and settings.
No reinstall, new sign-in or blanket default reset is needed. Eligible managed
public rooms drain independently when empty; occupied siblings keep playing.
The controller switches after required closure evidence and resumes saved
running-room intent. Deliberately stopped slots remain stopped.

Private booking guards or retained unrelated maintenance evidence can retain the
supported all-rooms-empty admission path. Do not delete or hand-edit those
records to force an independent drain. An offered/downloaded artifact is not an
installed host; actual room versions and native receipts prove adoption.
Pre-existing uncertain ownership still needs native verification before startup
or installation can be admitted.

The release retains configured recreation intervals, including existing
30-minute settings. It does not add player commands, expand shared moderation or
activate self-service booking. A separate initial/recreated-chat-access failure
under local pacing deferral remains follow-up work; this release does not claim
to eliminate every provider or shared-referee transport failure.

## Qualification and rollout

The exact artifact passed protected installed-runtime qualification with all six
full catalogs and a signed feed. An empty sibling closed while another worker
continued a match. Guarded native completion recovered an interrupted upgrade.
Activity included joins/leaves, commands and replies, completed matches and chat
reconnect. An eight-second SQLite exclusive lock preserved both workers and the
controller. Both intended aliases completed two natural 30-minute recreations
with the same workers; reserved slots remained off. Configuration/revisions and
intent were unchanged, generated room configs differed only in version stamps,
and a subsequent update check recognized the new version without a downgrade.

Tests used invented identities over loopback TLS/WebSocket fixtures on a
disposable donor. Failed fixture/plumbing and activity-sequencing attempts were
retained and corrected. Targeted affected regressions passed; the full repository
suite was not repeated after the localized changes. Nine runtime source files
changed, with no runtime files added/removed and unchanged dependency/storage
contracts. The published runtime's matching source is its version tag.

Production offering uses an owner-first gate: actual gameplay and the next
natural recreation boundary are verified before promoting the identical
artifact to community hosts. Current per-host adoption and any genuine ownership
exception belong in the wiki rollout checkpoint, not in a claim that every host
has already upgraded.

Runtime SHA-256:
`510161a436b721ecce0c4b22f75a4cc69e853f049a649f3c42b003e106131794`

## Separate website and hub maintenance

The central lobby-link exporter now derives labels/guides from current room
ruleset, key count and difficulty mode. Moving Taiko into a former 7K slot no
longer leaves its link labeled Mania. If the owner privacy source is unavailable,
links, guides and configured labels are hidden. This web change requires no
host/room restart and does not change the frozen client runtime. The link exporter
now follows the privacy-filtered ten-second live feed rather than the older
five-minute snapshot. Its path watcher follows that feed and all room configs,
so room links update after recreation. Stale live input hides links; privacy
source failure hides configured labels and guides as well.

The shared-ratings interruption was separately repaired by verified archival of
older operational audit rows, retaining canonical ratings. All three hosts
subsequently submitted rating events. That operational repair is not a new
ratings feature in this package.
