# Shared feedback operator guide (M4)

These instructions accompany the integrated owner-hub pilot, including current
qualified private releases. The original dev7 does not contain M4. Upgrade through OWNER_HUB_UPGRADE.md and follow
SHARED_HUB_TEST.md for the consenting one-room pilot. Enrollment, contribution,
historical seeding and network selection remain separate explicit choices. Owner
production adoption requires the qualified migration and explicit crossover
approval; these instructions do not authorize importing its live ratings.

## Local capture migration

The protected administrator supports `ratings-migrate` and `ratings-status` for
an explicit installed instance. Migration requires disabled/stopped lobby
and network services, clean room ownership, and no dedicated-user processes.
They do not stop services automatically. Integrity is checked at the registered
service UID first; SQLite writes then run with that UID and no supplementary
groups while the administrator retains the owner and network-admin locks.

```sh
sudo /usr/bin/python3.12 -I -S "$pilot_release/poolrotater_host/pilot_admin_entry.py" \
  --instance first ratings-migrate
sudo /usr/bin/python3.12 -I -S "$pilot_release/poolrotater_host/pilot_admin_entry.py" \
  --instance first ratings-status
```

Migration preserves a private whole-ledger recovery backup and records its hash
before publication. Shadow votes may exist in that private backup, but are never
exported as player feedback. Schema preparation and real vote/outbox writes use
the same ledger; full SQLite synchronization is enabled. Initial migration does
not convert historical votes into new events. Historical seeding is a separate
M4 batch with an explicit capture boundary.

If interrupted, `migration_phase: prepared` blocks worker startup. Retry the
same stopped migration to complete it. Do not delete its receipt or replace its
backup. A mismatched backup, foreign installation/generation, or restored old
schema requires review; it is not silently adopted. Use the separate restored-stream and canonical-epoch review commands below;
migration alone does not reconcile a changed authority or historical backlog.

## Dedicated central hub

The local hub administrator supports `ratings-prepare`, `ratings-grant` and
`ratings-inspect`. The schema migration holds the same server lease as `serve`,
and request threads join before that lease is released. It preserves a private
pre-M4 backup, fences interrupted migration and supports an exact repeat.

```sh
python -m poolrotater_host.network.admin --root /PRIVATE/INVENTED/HUB ratings-prepare
python -m poolrotater_host.network.admin --root /PRIVATE/INVENTED/HUB ratings-grant
python -m poolrotater_host.network.admin --root /PRIVATE/INVENTED/HUB ratings-inspect
```

`ratings-grant` reads a JSON object from private stdin with exactly `host_id` and
`mode`. Modes are `observe`, `contribute`, or `disabled`. Ordinary M3 enrollment
does not grant ratings scopes. An explicit observe-only grant quarantines submissions without affecting
canonical votes. Ordinary M3 enrollment alone grants no ratings scopes. Explicit contribute
grants submit/query scopes; disabled removes them. Rotation preserves explicit
grants. Credential revocation stops new ingestion without erasing prior votes.
Previously quarantined events remain quarantined on retry; granting contribution
does not turn an old captured opinion into fresh intent.

Only installation accountability is authenticated. Hosts' claims that a player
typed a vote are not cryptographically attested. Initial contribution quotas are
500 events per minute and5,000 per rolling day per host, plus200 per rolling day
per claimed player across hosts; excess contributions quarantine. HTTP admission
is separately bounded. The bounded batch and quota behavior are covered by technical tests; operational
calibration for real hosts remains subsequent deployment work.

The combined pilot client uploads at most100 events per step. Its nominal
30-minute scheduler sends at most4800 events in a half-open24-hour period and
preserves deadlines across restarts. After a committed acknowledgment, a
transient reconciliation query failure reports `query-deferred` and keeps the
normal upload deadline, rather than advancing new backlog at retry speed.
This does not guarantee quotas for repeated manual syncs, arbitrary retry/clock
movement or the global200-per-player limit. Keep owner historical opinions local
and use independent weighting until eligible central acceptance is verified or
explicitly reconciled. Do not repeatedly flush a seed backlog manually.

## Conflict and consumption status

See `RATINGS_CONFLICT_PROTOCOL.md` and `RATINGS_API_V1.json`. Upload acknowledgments
are per event; retries never apply a second effect. A state query can only resolve
keys associated with event IDs already handled for that requesting host, up to100
at once. Support output contains counts/revisions, not voter IDs, room context,
event bodies, endpoint URLs or credentials.

Player commands commit locally before acknowledging. Conflict reconciliation only
updates revision knowledge; an actual repeated player command can create a fresh
event expressing the same local opinion. It cannot rewrite the old conflict.

Installed periodic sync, historical seed, suppression, aggregate download and the
canonical reader passed protected installed donor qualification. Independent/local
weighting remains the default; explicit network-reader selection uses canonical
snapshots only. Central acceptance reports aggregate inclusion unconfirmed until
a verified snapshot is activated.
Do not claim shared weighting is active from an upload acknowledgment.

## Explicit sharing and scheduling

For the shortened protected commands below, define this shell function using the
reviewed installed release path. It always names the same instance:

```sh
pilot_ratings() {
  sudo /usr/bin/python3.12 -I -S "$pilot_release/poolrotater_host/pilot_admin_entry.py" \
    --instance first "ratings-$@"
}
```


After stopped ratings-migrate and separately reviewed M3 enrollment, have the
maintainer grant your enrolled host central `contribute` permission before
enabling local sharing. An observe-only grant can quarantine input permanently;
a later contribution grant does not promote those old events. Historical seed
permission is a separate optional decision. Then invoke
`ratings-status` and `ratings-support` include a `progress` object distinguishing
locally captured events, queued/retryable uploads, central accepted/already-accepted
acknowledgments, nonaccepted outcomes and a fully verified shared snapshot. Event
counts are not distinct voter counts or final opinions. An acknowledgment is not
proof of aggregate inclusion. Configured reader eligibility is separate from a
verified observation that running workers use network weights. The diagnostics
never silently switch selection mode or grant contribution permission.

`pilot_ratings configure --ratings-stdin` with the closed JSON
`{"share_ratings":true}` (or false) on stdin. No provider token belongs in this input.
Sharing defaults absent/off. `pilot_ratings sync` performs one bounded
upload/query; `pilot_ratings status` and `pilot_ratings support` give counts and fixed
health categories while rooms can run. These actions neither start nor stop services.

The optional disabled-by-default network unit schedules ratings every30min plus
bounded jitter, with persisted5s exponential retry up to300s. Authority/local review
wait30min. Its health receipt is distinct from heartbeat. Changing sharing false stops
future requests once any already-dispatched bounded request finishes; a lost reply may
already have committed centrally. This action does not retract prior contributions.
Upload acceptance does not prove aggregate inclusion or change the separately
configured reader mode.

Aggregate fetching has a separate persisted deadline and health receipt. In network
mode partial downloads continue in bounded2-page requests every5s plus jitter;
complete activation returns to30min plus jitter. This does not accelerate vote uploads
or canonical queries. Fetch outages use independent persisted backoff; a corrupt
fetch receipt is retained for review while heartbeat and upload scheduling continue.
Count-only support includes fetch phase/error/deadline/pending pages.

## Fresh feedback and historical backlog

Keep historical seeding out of the first independent tester run. New real player
votes can then be checked without waiting behind a large imported history. Events
upload in sequence order, including historical seed events. At100 events per
nominal30-minute interval, a retained7,479-event backlog takes roughly37.5 hours
plus jitter and outages before later votes are reached. The maintainer must check
current safe outbox counts rather than promise that a new owner vote uploads on
the next tick. Repeated manual calls are not a safe backlog-clearing procedure.

Do not move later votes ahead of earlier sequences: the hub's monotonic stream
boundary can reject the skipped earlier events as restored sequences. Keep owner
history/local weighting intact while the queue drains normally. A first near-term
shared proof can be a fresh consenting tester vote and its verified snapshot on
the owner installation; confirm later owner feedback separately once reached.

## Restored hosts and replacement enrollment

Stop and disable lobby/network units before `pilot_ratings recover-stream`. It queries
an authenticated same-epoch stream high-water and monotonically raises the local
sequence before fresh votes. Existing event IDs/payloads/outcomes are preserved.
A crash before profile publication safely repeats; new generation/epoch cannot be
adopted through this operation. Keep state for review rather than resetting it.

For revoked-host reenrollment, the hub administrator explicitly invokes
`ratings-link-replacement` with private stdin host_id/previous_host_id. Both enrollments
must match operator/account/installation/generation and the old host must be revoked.
Recovery also requires an explicit contribute/query grant before stream/epoch
review. The hub may downgrade to observe afterward; local sharing may remain off.
Only that reviewed lineage enables replay/query of previous private event history.
Revocation remains effective for the old credential, and does not erase prior votes.
Canonical epoch restore and deletion-policy replay are covered by the installed
donor matrix and local interruption/anti-resurrection tests.

## Historical import (invented fixtures only for this qualification)

`pilot_ratings seed` requires the same stopped/disabled writer
proof as migration. It reads the fixed immutable pre-M4 backup and verifies its
receipt/hash; no path to a production DB is accepted. It preserves eligible neutral
opinions, applies historical/current policy, excludes shadow votes, and skips keys
with post-boundary intent. Repeat-safe immutable seed events join the durable outbox.
The hub explicitly grants `ratings-grant-seed` via private stdin host_id/allow; normal
contribution permission is insufficient. Uploading before that grant quarantines
those events permanently. Seed import never replaces an existing canonical opinion.
This tooling is qualified only with invented historical ledgers in development.

## Suppression and deletion recovery

Root-local hub `ratings-policy` accepts subject/effect and the exact applicable
user_id, beatmap_id or host_id on private stdin. Subjects user/key/host support
suppress/allow; user also supports irreversible logical delete. No remote admin
route exists. Suppression removes affected effective source contributions. Allowing
again does not restore old sources; fresh actual input must know the new revision,
including previously unseen keys. Credential revocation alone never deletes votes.

The private ratings-policy.json journal uses keyed target digests. Keep its latest
copy independently of DB snapshots. A pending policy blocks ratings processing;
`ratings-policy-recover` replays it. M4 restore private stdin must include source
and ratings_policy paths; an old database alone is refused. Prefix hashes bind the
journal to the snapshot and newer suppression/deletion replay before authority reset.
The administrator must select the latest trusted journal; an offline backup cannot
know that newer operations exist elsewhere. Do not publish this journal or backups.

Deletion removes active voter identifiers/opinions/payloads from logical tables,
retaining replay hashes/host accountability and a permanent keyed exclusion. It
does not physically wipe free pages, WAL, storage devices or older external copies.
A separate operator backup/retention procedure is required before operational use.

## Aggregate fetch and consumption choice

`pilot_ratings fetch` downloads up to2 pages from the explicitly
trusted enrolled hub, resumes cached pages and activates only a complete verified
canonical snapshot. It requires a configured ratings binding but does not enable
sharing, start a service or read provider tokens. Explicit ratings-read permission
is separate from private canonical queries. Pending/error downloads retain cache.
Unchanged verified pages can be reused across revisions, including interrupted
downloads. Owned current/previous versions and an in-progress target are retained;
unknown folders or files are preserved for review. Writes require16MiB of remaining
disk headroom plus the next page. Disk refusal preserves the active cache. The hub
retains its current and previous published versions; clients refetch the manifest
when an older page has been pruned. Counts contain no player identifiers.

`pilot_ratings reader-configure --ratings-stdin` takes mode
independent/network and requires stopped/disabled writers. Network mode is refused
while the immutable packaged consumption-qualification gate is closed. Shadow and
donor core evidence opened that gate in the preceding development candidate.
The combined dev8 artifact must independently pass installed synchronization,
snapshot and outage checks; earlier M4 evidence does not qualify a new release.
Independent mode
remains the default. In network mode all
pool styles and vote displays use canonical counts only. Local/shadow opinions are
never added to global tallies. The formula is unchanged; cold/unavailable cache is
neutral, outages keep last valid data. Support reports mode/cache phase/watermark.

The previous cache may be used after corruption only if it respects every policy
watermark already learned. This avoids reintroducing known-deleted contributions.
The learned floor is persisted as soon as a verified manifest arrives, before any
page download. Until the updated snapshot is complete, earlier counts below that
floor become unavailable and network weighting is neutral. Restart, page outages
and pointer recovery preserve this exclusion. Epoch review archives and clears
the old authority's floor along with its activation pointers.
Cached pre-change data during a disconnected period remains last-known information;
a host cannot discover an unseen central policy change while offline. A new epoch
or lower watermark requires explicit restore review before activation.

## Canonical hub epoch restoration

After restoring a hub, all old authority is revoked. Reenroll locally and obtain
an explicit root-reviewed matching-identity replacement lineage first. Stop/disable
writers, then `pilot_ratings review-epoch --ratings-stdin` with
expected_profile_sha256, expected_new_epoch and acknowledge_quarantined_backlog:true.
It creates a consistent private recovery backup before changing state. Do not
supply provider/OAuth tokens. Wrong digest/epoch or unreviewed lineage is refused.

Old unsent/retryable input is explicitly quarantined; old accepted acknowledgments
remain historical facts, not proof of current aggregate membership. Payloads are
never rewritten into the new epoch. Revision knowledge and old snapshot activation
are cleared; archived snapshots cannot silently mix with new authority. New actual
commands can reaffirm opinions; review alone does not generate votes. Pending review
blocks startup/sync and repeats safely after interruption. Keep the exact receipt,
profile and ledger backup for recovery. Independent latest-policy retention is still
required on the central restore, including irreversible user deletion exclusions.
