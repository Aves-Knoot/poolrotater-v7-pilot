# M4 conflict protocol and decision spike

The implemented decision uses server logical revisions plus a narrowly checked
same-stream predecessor. Client clocks and upload receipt order do not establish
newer player intent. Canonical identity is still one `(beatmap_id,user_id)` pair;
neutral is a value. The decision is covered by local protocol/repository tests, authenticated TLS
integration and the protected two-host donor matrix. Final evidence and limits are
recorded in M4_ACCEPTANCE.md.

| Approach | Offline opposing backlog | Sequential offline changes | Cost |
| --- | --- | --- | --- |
| Last server receipt wins | Old delayed votes overwrite newer opinions | Works | Violates freshness requirement |
| Largest client timestamp wins | Skewed/untrusted clocks overwrite newer opinions | Sometimes | Cannot establish global order |
| Strict known revision only | Conflicts safely | Second queued change conflicts after first advances revision | Repeated player input even without another host |
| Revision plus current accepted predecessor | Conflicts safely across hosts | Works while the exact predecessor remains current | Cross-host conflict still requires fresh player input |

`poolrotater_host/ratings/conflict.py` implements the last row. The repository
must validate the closed event contract, authenticate its stream, enforce policy,
deduplicate immutable event IDs and detect sequence reuse **before** deciding.

1. Suppressed/deleted keys reject input, even with a known revision.
2. A captured revision equal to the current revision accepts new intent. For a
   missing key, unknown or explicit revision0 can create the first opinion.
3. Otherwise, only the exact still-current accepted predecessor can authorize a
   sequential change. It must have the same installation, generation, player and
   beatmap, a lower sequence, and the current canonical event ID/revision.
4. An unknown/stale same-value opinion is superseded without changing canonical
   state. It cannot confer predecessor authority over a subsequent opposing vote.
5. All other stale/unknown revisions conflict. A state query informs future input;
   it never edits a stored event or turns its rejection into a new player action.

Retries return the original committed per-event outcome; an accepted duplicate
can be labeled already-accepted without applying a second effect. Different
content under the same event ID is rejected. Restored clients cannot change an
old event's generation or reuse a sequence for different payloads. Fresh stream
recovery must preserve/quarantine old events and obtain fresh revision knowledge.

If another host changes an opinion during an offline sequence, subsequent queued
changes conflict unless already equivalent. The local opinion remains durable.
After reconciliation, the player can deliberately submit it again as new intent.
Command acknowledgment remains local and nonblocking; sync status reports the
conflict. Automated replay/rebase is prohibited.

Evidence: `tests/test_rating_conflict.py` compares receipt/timestamp alternatives
and covers missing/restored history, simultaneous hosts, predecessor interference,
wrong key/generation/sequence, neutral replacement, suppression and skew/retries.
M4_ACCEPTANCE.md records the additional repository/TLS/installed-agent/donor
evidence; pure decision tests alone do not prove those integration properties.
