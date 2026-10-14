# Public community display protocol v2

This extension adds public room settings and qualified match observations without
changing the closed v1 enrollment, heartbeat, directory, ratings or credential
contracts. It grants no room-control authority. The associated release is staged;
this document does not mean the running central hub or operators have upgraded.

## Routes and authority

| Method and path | Authentication | Contract |
| --- | --- | --- |
| `GET /v2/capabilities` | Public | Exact advertisement below; no query parameters |
| `POST /v2/hosts/self/heartbeat` | Existing host bearer credential, `heartbeat` scope | Closed v2 heartbeat; no query parameters |
| `GET /v2/rooms?offset=0&limit=50` | Public | Closed v2 directory; only those pagination parameters |

The existing invitation, enrollment, credential rotation, consent, revocation,
ratings grants and scopes remain unchanged. Heartbeats contain at most four
rooms; heartbeat request bodies and operator transport replies are bounded to
65,536 bytes. The website directory reader separately caps responses at
262,144 bytes and withdraws community data on overflow. Directory limit is 1–100,
offset is 0–1,000,000, and repeated or unexpected query fields are refused.
Existing heartbeat sequencing, replay checks, five-second receipt quota and
public request quotas also apply.

`GET /v2/capabilities` returns exactly:

```json
{
  "ok": true,
  "protocol_version": 2,
  "display_version": 1,
  "heartbeat_path": "/v2/hosts/self/heartbeat",
  "directory_path": "/v2/rooms"
}
```

A new client probes this read-only endpoint through its already configured,
pinned HTTPS transport. It uses v1 only when that trusted endpoint explicitly
returns HTTP 404 with `{"ok":false,"error":"route_not_found"}`. Timeout, TLS
failure, unauthorized response, malformed advertisement, unknown fields and
other errors do not authorize fallback. Redirects and ambient proxy/CA settings
remain refused. A mutation failure never causes an immediate retry through a
different endpoint.

## Heartbeat

See [the complete invented example](../examples/network-heartbeat.v2.json) and
[the closed schema](../schemas/network.display.v2.schema.json). Every listed
field is required; unknown evidence uses an explicit `null`.

The top-level fields are exactly:

```text
protocol_version: 2
installation_id: canonical UUID
generation: canonical UUID
sequence: durable nonnegative integer, at most 2^53-1
code_version: existing version string
catalog_version: existing content digest or "unavailable"
sent_at: operator wall-clock Unix seconds at publication
rooms: list of zero to four public room observations
```

Each room retains every v1 field:

```text
alias, osu_room_id, preset, state, occupants, visibility, title,
min_stars, max_stars, observed_at, code_version, catalog_version, current_map
```

It adds exactly one field, `display`, which is `null` for legacy or unknown
evidence, or has this shape:

```json
{
  "settings": {
    "ruleset_id": 0,
    "keys": null,
    "auto_difficulty": true,
    "selection_style": "variety",
    "allowed_mods": ["NF", "HD"],
    "required_mods": ["DT"],
    "freestyle": false,
    "countdown_seconds": 15
  },
  "playing_players": 1,
  "match": {
    "playlist_item_id": 99,
    "beatmap_id": 100,
    "started_at": 1792000000,
    "clock_rate": 1.5,
    "participants": null
  }
}
```

`current_map` stays `null` or the closed v1 object
`{beatmap_id,title,stars}`. Beatmapset identity, original song duration and cover
URLs are resolved centrally by the website from its display index, rather than
accepted as operator-provided duration/image overrides. Public titles and map
labels are bounded to 160 characters at projection; truncating long valid
labels does not discard an otherwise valid room.

### Settings and counts

`ruleset_id` is integer 0–3. Standard presets require 0 and `keys:null`; Mania
presets require 3 and either unknown keys (`null`) or their actual configured
4/7 keys. Contradictory preset/ruleset/key combinations are refused.
`auto_difficulty` and `freestyle` are booleans. `selection_style` is bounded to
80 non-control characters. Mods are distinct uppercase ASCII alphanumeric
acronyms, at most 32 per list and 16 characters per acronym. Countdown seconds
are finite and bounded to 0–600.

`occupants` is the observed room roster count, not proof that everyone is
playing. `playing_players` is the current count of explicitly observed playing
roster states, excluding known referee/operator identities. It is nullable when
roster evidence is incomplete, must not exceed occupants, and is cleared outside
PLAYING. No user identity, username or roster list is uploaded.

`match.participants` represents a qualified count at match start, not a count of
eligible people in the lobby. The current native source does not prove that
start-time list, so it emits **null**. Its existing `playing_player_ids` includes
eligible unready/AFK people and is deliberately not presented as actual match
participation. A nullable count can range from 0 to 128.

### Match evidence and estimated timing

A match object is emitted only from an accepted native `MatchStarted` event,
with the frozen playing map and playlist item. Its `started_at` is the operator's
wall-clock time when that event was observed; it is **not an exact upstream game
clock**. It is never map-selection time, countdown-start time, a periodic refresh
time or a synthesized missing-start completion. Delayed provider event delivery
can make the estimate late.

The match is bound to the current room and the same beatmap, playlist item and
start observation. A repeated/replayed item has a new start observation; a new
room ID gives a separate room identity. A map/item mismatch or reset invalidates
the observation. Completion, abort, idle/starting/draining transitions and stale
data clear timing rather than continuing an old song's progress. The wire
validator requires PLAYING, a matching current map, and start time no later than
room observation plus five seconds.

`clock_rate` is `null` unless explicit native playlist settings prove a common
speed. Required DT/NC default to 1.5, required HT/DC to 0.75; a known finite
`speed_change` between 0.25 and 3 is accepted. Known non-clock mods, including
NM and key mods, retain 1.0. Missing mod evidence, freestyle, optional speed mods,
unknown mods, conflicting speed mods and unsupported speed settings yield
unknown timing. The rate is frozen at match start, so later playlist edits cannot
change a running match's estimate. Optional player-dependent speeds do not
justify a common room progress bar.

## Receipts and public directory

The heartbeat receipt is unchanged in shape:

```json
{"ok":true,"sequence":7,"replayed":false,"received_at":1792000014}
```

`received_at` is the central hub wall clock. The v2 directory top-level fields
are exactly:

```text
ok: true
protocol_version: 2
server_epoch: UUID
generated_at: hub wall-clock Unix seconds
rooms: at most limit rows
next_offset: null or the next numeric offset
```

Directory rows contain the room fields above, plus exactly the existing public
fields `host_id`, `operator`, `age_seconds`, `freshness`, `join_url` and
`distribution:"self-reported"`. Only a directory match object adds this field:

```text
clock_skew_seconds: null or finite number between -30 and +30
```

The hub estimates correction as the accepting receipt's `received_at - sent_at`.
The original `started_at` and `observed_at` remain operator wall-clock values.
When absolute correction exceeds 30 seconds, `clock_skew_seconds` is null and the
website must hide progress. Otherwise estimated corrected start is
`started_at + clock_skew_seconds`; effective duration is original song duration
divided by `clock_rate`. Apply the same correction to the observation when
checking timing consistency.

This one-way estimate includes transport delay; it is **not NTP synchronization**
and does not remove uncertainty in delivery of the provider's start event.
Consumers must label progress estimated, reject future/inconsistent timing,
clamp elapsed time to effective duration, and hide it when map, match, duration,
clock rate, freshness or correction is unknown. A delayed heartbeat carries the
original start observation; receipt time never becomes a replacement start.

The hub's freshness age uses the older of the accepting receipt and corrected
room observation when correction qualifies. Otherwise it uses the original
observation conservatively. At 180 seconds it reports `state:"unavailable"`,
null occupants/current map/join link, and null display match/playing count. At
600 seconds it hides the room. Revoked hosts, expired credentials, withdrawn
listing consent and private or unconfirmed rooms are excluded. Settings alone
can remain on a stale card; they never keep a join link or timer alive.

Older clients on the new hub still publish v1 heartbeats and appear in v2 with
`display:null`. Their centrally known map art/duration may be displayed, but
missing match/clock evidence must not invent progress. `/v1/rooms` strips display
extensions even for new clients, preserving its closed row shape.

## Pending publications and rollback

Heartbeat state keeps the existing local format and durable sequence. New
observations supersede uncertain older heartbeats using a strictly higher
sequence; the client never replays formerly public data after a private-room or
listing withdrawal.

- A pending v1 heartbeat is resolved by a fresh higher-sequence v1 observation
  and accepted receipt before negotiation upgrades to v2.
- A pending v2 heartbeat normally resolves through a fresh higher-sequence v2
  observation. If a read-only capability probe explicitly confirms an old hub's
  route-not-found 404, a fresh higher-sequence v1 observation can resolve it.
  There is no immediate fallback/retry after an uncertain mutation.
- Failed negotiation or publication retains recoverable pending state. Do not
  clear journals, reenroll or copy credentials to bypass this condition.

There is no database schema migration. For v2 only, the hub's internal heartbeat
JSON keeps `rooms` limited to the exact v1 fields and stores display evidence in
a separate top-level `display_rooms` extension keyed by alias. The durable
payload fingerprint remains the original received **wire** fingerprint; it is
not recalculated from the internal envelope. Stored v1 payloads are not rewritten.
An actual frozen dev10 reader was qualified against the v2 envelope: it ignored
the extension and returned its original closed v1 directory. A later accepted
higher-sequence v1 heartbeat replaced the extension safely. This describes
internal storage, **not additional accepted wire fields**.

## Activation and validation limits

Activate the qualified central hub build first in a separately authorized
maintenance window; preserve its database, epoch, credentials, grants and TLS
trust. Then upgrade operator runtimes in clean stopped windows using the
release's supported state-preserving upgrade. Existing dev9/dev10 clients can
remain on v1 during that rollout. Neither this document nor frontend deployment
authorizes restarting currently running owner or Xikeon rooms/services.

Source tests cover strict contracts, legacy/new clients, pending publication
withdrawals, clock bounds, map/state binding, native event/completion observations,
long labels, simultaneous hosts and real loopback HTTPS. The frozen old reader
was also checked against new storage. Installed full-catalog donor qualification
and final release evidence are recorded separately by the maintainer. No source,
schema or simulated-provider check proves actual remote gameplay timing accuracy;
that remains an explicit follow-up after coordinated operator activation.

## Rebuilding central map display metadata

When a full prepared catalog changes, rebuild the central index using
`scripts/build_community_display_index.py`. This is separate from operator
catalog installation. Minimal operator exports intentionally omit set identity
and original song duration; use the central immutable **full source snapshots**
associated with the prepared manifests. Their approved
`manifest.sources.catalog.sha256` pins bind those snapshots. Never run this
against a collector's actively written database or add API requests to refreshes.

Provide an independently reviewed, SHA-pinned JSON manifest with exactly three
sources. Paths must be absolute. Mode/table pairs are fixed:

```json
{
  "format": 1,
  "sources": [
    {"mode":"standard","table":"beatmaps","path":"/staging/full/standard_farm.db","sha256":"APPROVED_SOURCE_SHA256"},
    {"mode":"mania_4k","table":"maps","path":"/staging/full/mania_4k.db","sha256":"APPROVED_SOURCE_SHA256"},
    {"mode":"mania_7k","table":"maps","path":"/staging/full/mania_7k.db","sha256":"APPROVED_SOURCE_SHA256"}
  ]
}
```

The placeholders above must be replaced by the approved lowercase 64-character
source digests. Supply the independently approved manifest digest as well:

```bash
nice -n 15 python3.12 scripts/build_community_display_index.py \
  --sources-json /staging/display-sources.json \
  --sources-sha256 APPROVED_SOURCES_MANIFEST_SHA256 \
  --destination /staging/display-next.db \
  --report /staging/display-next-report.json
```

For development validation, run the same command through `scripts/offline` using
isolated source copies and temporary outputs. The builder performs no production
promotion, network collection, operator-state access or service operation.

Only these nine columns enter `display_maps`:

```text
mode, beatmap_id, beatmapset_id, title, artist, version,
total_length, hit_length, sr
```

No source SQLite pages, other tables, player IDs, scores, analysis history or
extra columns are copied. Missing display metadata remains unknown. Map 1549496
is always excluded; repeat `--exclude-beatmap-id ID` for additional explicitly
approved exclusions. Duplicate IDs within a mode, changed inputs, unsettled
SQLite sidecars, symlinks, nonregular files, corrupt databases, missing columns,
invalid numeric metadata and unapproved hashes refuse the build.

The tool checks source identity and SHA before and after extraction. It produces
only fresh outputs, never overwrites an existing index/report, caps the index at
64 MiB, and sets both completed files to mode 0444. Its report records source
pins/counts, exclusions, output bytes and output SHA. The current full-source
qualification exported 105,502 Standard, 19,334 4K and 4,347 7K rows. Its disposable
11,857,920-byte output was removed; the report is evidence, not a deployed index.

### Separate guarded promotion

Promotion is a root custody operation, after review of the new report. It
requires **both** an independently approved report SHA and the expected currently
installed index SHA. Do not substitute a just-computed old-index hash merely to
bypass a mismatch. Retain the previous index for rollback outside the public
webroot.

A qualified promotion must:

1. Require root and regular, nonsymlink files/ancestors; refuse hardlinks and
   writable public destination ancestry.
2. Verify the report against its approved SHA; verify the new index against the
   report's output SHA, its closed nine-column schema, integrity and row counts.
3. Verify the current `/var/lib/poolrotator-wiki-live/display.db` against the
   independently supplied expected old SHA.
4. Copy the candidate to an exclusive temporary file in that same destination
   directory, verify the copy's SHA, set root ownership/mode 0444 and fsync it.
5. Recheck current-file identity and expected old SHA immediately before an
   atomic `os.replace()` of the temporary file, then fsync the directory.
   Refuse a changed target instead of overwriting someone else's update.
6. Verify the installed file's SHA, owner and mode; record the old/new index and
   report pins. Remove only the promoter's owned temporary file on refusal.

No restart is needed for index-only promotion. The publisher opens a fresh
read-only index for each refresh and checks its identity during lookup. An
in-progress refresh that detects replacement uses its neutral metadata fallback;
the following refresh reads the new inode, avoiding a retained old duration/art
cache. Ordinary frontend refreshes continue without an osu! API request.

The public build tool intentionally does not perform this live replacement.
Schedule and implement the separately reviewed root promotion when the next
central catalog update actually requires it; current services and their index
remain unchanged during this development batch.
