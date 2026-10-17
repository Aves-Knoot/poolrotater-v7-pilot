# v7.1.6: responsive shared map feedback

Fixes saved `.good`/`.bad` votes being thanked with an unchanged shared tally.
Owner and community installations use the same behavior and the familiar reply:
`Thanks for the feedback! <map> — X good / Y bad.` No backend explanation is
added to player chat.

New live votes wake the independent background ratings agent on its five-second
local check instead of waiting for the normal 30-minute delivery deadline. A
successful upload prompts a verified snapshot refresh. The command handler makes
no hub/provider request and does not wait for that refresh. Its bounded reply is
sent once a verified snapshot covers the acknowledgment watermark. During an
outage, the vote remains durable and the reply simply thanks the player for their
opinion, without presenting a stale count. Neutral feedback and independent-mode
replies retain their previous behavior.

The canonical aggregate remains the only shared selection source. Local votes
are never added on top of it. One person voting on multiple hosts still counts
once, and known-revision opinion changes replace the existing vote. Existing
moderation policies, sharing opt-outs, conflict rules, immutable event IDs,
recovery gates and provider API pacing remain unchanged. A persisted delivery
budget bounds scheduled new identities to 100 plus 100 per 30 minutes; lost-response
retries reuse reservations. A restart cannot refill that budget. Manual sync
continues using its existing explicit operator semantics.

This is a controller update because the network agent also changes. Automatic
application requires all configured rooms to pass the existing fresh empty/idle
checks and admission gate. It must not be sent as a worker-only rolling patch.
No existing OAuth, Discord, room configuration, catalogs, history, enrollment,
sharing choices or automatic-update policy are reset. No database schema or
third-party dependency changes. It includes the published v7.1.5 refresh repair;
the separately paused maintenance/admin work remains excluded.

Validation: 313 distinct frozen regression cases passed; the root-only migration
case was skipped in the ordinary UID sandbox. Its existing root/service-UID
upgrade fixtures separately passed from v7.1.2 and v7.1.5 with complete prepared
catalogs (105,502 Standard, 19,334 4K, 4,347 7K rows). These verify byte-preserved
configuration/catalog/credential files, unchanged ledger schema/rows and unchanged
incident ownership state. Feedback/selection checks also use the full catalogs.
A test-driver relative-import error was corrected and the affected test passed;
passing suites were not repeated. No actual provider/room gameplay test or donor
VPS test was performed for this batch, and the entire repository suite was not
rerun.
