# Shared statistics repair — v7.2.10

## Intended behavior

A `.stats <username>` command reads the complete local aggregate replica,
including on community hosts. It performs no command-thread HTTP request. The
existing background refresh, privacy lease, hidden-player preferences, tracked
PP deduplication and host binding remain authoritative.

The v7.2.9 worker guard admitted the small cache index but not the separately
published immutable `replica.sqlite3`. The command swallowed the denied read,
then reported a miss or waited until its pending reply expired. The repair
admits only private, service-owned, completed replicas under the bound host's
`state/activity-replica-downloads/<64-hex-content-id>/replica.sqlite3`, opened
with `mode=ro&immutable=1`. Staging files, other paths, symlinks and writes stay
refused. The rule is evaluated on every open so an agent refresh does not need
a worker restart.

Catch hosts use the additive v2 replica endpoint. Both the parent HTTPS route
allowlist and its isolated worker now recognize v2 with the same bounded page
size and gzip support as v1. Legacy v1 remains compatible.

## Private participation

Known private-room participation contributes playtime, joins/leaves, completed
map participation and recorded PP gains to player aggregates and website stats.
Room visibility is still required to attribute a ruleset; unknown visibility
remains excluded. Public room lists, status panels, links, passwords and chat
exposure do not change. Account-wide hidden-stat preferences still apply.

Independent private presence/join/leave cursors backfill authoritative records
that an older release already skipped after the existing sharing boundary.
They do not reset public cursors, change existing public event IDs, import
unreviewed legacy cumulative history or double-upload acknowledged records.
History review retains its public-room-only rules for old ambiguous evidence.
The wire schema is unchanged, allowing updated hosts to upload to the existing
hub, which already aggregates these event types into player totals.

## Greetings

`Say hey to saycey` is part of the distributable defaults. The maintainer also
applied the greeting through authenticated typed live operations to all eight
current owner/community slots. Saved and effective snapshots confirmed all seven
existing greetings match across both hosts. No room was restarted.

## Delivery and qualification

This is a controller update because `rolling/storage.py` and the background
transport/collector change. Normal automatic installation waits for the host's
safe empty-room boundary; a forced occupied-room restart is unnecessary.
Explicit stopped slots, private settings, titles, OAuth, catalogs and history
must remain intact. Supported predecessor transitions are pinned to the exact
published v7.2.6, v7.2.7, v7.2.8 and v7.2.9 runtime inventories.

Validation and publication receipts are kept in the maintainer's isolated
`validation/stats-repair` workspace. Actual public-provider gameplay is not
part of synthetic qualification. Publication/queue receipts distinguish a
prepared artifact from an installed host.

## First successor admission follow-up — v7.2.11

Live safe-queue admission exposed a separate v7.2.9/v7.2.10 root/service UID
read mismatch. The strict service-owned projection is now read under its service
identity. See UPDATE_QUEUE_ACTIVATION.md for affected-host activation. The stats
repair and private-participation behavior above are unchanged.
