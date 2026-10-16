# v7.1 shared map-rating qualification

This qualification reuses the existing native vote writer, immutable outbox,
Synchronizer, authenticated hub grants, canonical repository, snapshot publisher,
verified host cache, NetworkReader and native map-selection formula. No second
feedback system or replacement selection algorithm was introduced.

`tests/test_v71_ratings_parity.py` contains nine cross-host cases using two fresh
installed-layout fixture roots, representing owner and community accounts. Every
account, player, beatmap, invitation and credential is invented. They run in the
ordinary offline sandbox at nice 15 and never touch production services, real
rooms, catalogs, secrets or another operator's settings.

The qualification covers:

- Actual `.good`/`.bad` handlers and `.g`/`.b` aliases persist equivalent intent
  through RatingStore into the existing migrated durable outbox.
- Both synthetic hosts receive an explicit contribute grant and independently
  choose sharing and network consumption. Enrollment alone enables neither.
- Upload acceptance is followed by real canonical repository state, published
  snapshot contents, verified cache activation and identical readers on both
  installations. Successful transfer alone is not called aggregate acceptance.
- One player's same opinion reported by both hosts remains one canonical vote.
  That player's later known-revision vote replaces their earlier opinion.
- Pending local feedback is kept locally but is not added on top of a network
  snapshot. After upload and fetch it appears exactly once in both readers.
- A lost upload acknowledgment can be retried after restart without duplicating
  the accepted opinion. An outage keeps unsent votes and last-good canonical
  selection counts, then converges after reconnection.
- Real native Standard auto/fixed and native 4K/7K pool objects consume those
  shared tallies through `_rating_tallies` and `_weighted_pick`. The resulting
  selection weights match the retained native formula and other style/pass-count
  factors. Pending local negatives cannot leak into the canonical selection path.
- Independent consumption remains independent even after a snapshot is fetched;
  sharing can be disabled without implicitly changing consumption. A missing hub
  grant leaves the vote pending instead of pretending it affected an aggregate.

The transport fixture invokes the same production endpoint objects and enforces
snapshot-read credentials. It makes no socket or provider calls. These focused
cases use invented deterministic catalogs to exercise all four native pool types;
full owner-prepared catalog and installed HTTPS/donor qualification are separate
release gates. They do not claim actual player gameplay acceptance or that a
particular real host has enabled sharing/consumption.

## Activation and evidence

Each real operator must explicitly choose both `share_ratings` and the reader's
`mode`. Use the protected installed `ratings-configure` command with JSON
`{"share_ratings": true}` only after a bound hub contribute grant exists. Use
`ratings-reader-configure` with JSON `{"mode": "network"}` only in its documented
stopped/disabled maintenance window. The command itself enforces that boundary;
it does not stop occupied rooms to change the reader. Preserve an operator's
independent choice if they decline. Never modify another operator's consent from
the hub, a website directory flag, an installer default or an update.

After normal asynchronous upload and snapshot fetch, `ratings-support` separates
locally captured and queued events, central acknowledgments, a verified snapshot,
and configured selection source. Its runtime-reader-observation and aggregate
inclusion fields intentionally do not claim proof that the status command cannot
observe. For a real acceptance record, retain the redacted before/after vote,
acknowledgment, canonical key/revision or reviewed aggregate, snapshot identity
and reader observation. Do not post credentials or player-private event payloads.

During hub outages, player commands and map selection read local state. In network
mode the last verified canonical snapshot remains authoritative; there is no
local overlay that would later double-count a pending vote. A cold/unavailable
canonical cache uses neutral weights. Independent mode retains its existing
local feedback behavior. The native lower weight floor remains positive, so
negative feedback never makes an otherwise eligible pool mathematically empty.
