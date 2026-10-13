# One-room gameplay and shared hub feedback

Upgrade using OWNER_HUB_UPGRADE.md; retain the same account and instance. Record
version, timestamps/timezone and before-support. Confirm clean ownership, complete
catalog rows, valid local authorization, enrolled hub and agreed directory/ratings
choices. Fresh tester installs default to one room. No production owner crossover
or destructive outage/restore is part of this tester checklist.

1. **Start once.** Use the protected `start` entry in the agreed window. Send the
   room link. If startup is uncertain, retain state and return safe failure code/
   HTTP status before retrying; an upstream room can exist despite a local error.
2. **Active diagnostics.** While players are present, run `status`, `support`,
   `network-support`, `ratings-status` and `ratings-support`. They should return
   promptly without competing for the running service's account lease. Send the
   redacted results and elapsed times; no private pacing hash/account IDs.
3. **Gameplay (people required).** Play at least 3–5 maps. Try help, queue, skip,
   easier/harder, language selection, `.g`/`.b`, readiness and countdowns, AFK or
   unready companions with at least two players, joins/leaves and song spacing.
   Note actual maps/results and which checks were exercised; do not mark untried
   features passed. The referee account cannot participate as a player.
4. **Sharing.** First have the maintainer grant this enrolled host central
   `contribute` permission (and historical seed permission separately, if used).
   Only then configure local `share_ratings: true` and start the network agent.
   Enrollment or `observe` permission alone is insufficient: uploading before
   the grant quarantines opinions, and a later grant does not promote them.
   Submit a good and bad
   vote from actual players. Record the map IDs, approximate times and commands,
   without player identities/private chat. Capture outbox/sync/snapshot counters;
   the maintainer checks deduplication, shared totals and snapshot activation.
   A negative vote is valid feedback; changing a vote must not add a second copy.
   Keep independent/local map weighting for this first test. After the agreed
   sync interval, run the protected `ratings-status` and `ratings-support` entries.
   Use `ratings-fetch` explicitly to fetch the verified shared snapshot; repeat
   while it reports pending pages, then capture `ratings-support`. Automatic
   snapshot fetching is enabled only in network-reader mode. A manual sync is
   `ratings-sync`; coordinate one call rather than repeatedly flushing a backlog.
   The nominal upload is at most100 events per30-minute scheduler interval;
   manual calls and the global per-player quota need separate review. The
   maintainer must confirm eligible historical acceptance before any later
   explicit switch to network weighting.
5. **Directory.** If opted in, check the hub lists only this public room and its
   current range. Private/unknown rooms and player/chat data must not appear.
6. **Hub outage (coordinate first).** The maintainer performs a bounded hub-only
   outage. Keep playing; local map selection/voting/countdowns should continue.
   Return network/ratings support during outage and after reconnection. Pending
   contributions should settle without duplicate totals; a partial snapshot must
   not replace the last verified one. Do not stop other VPS services or tamper
   with ownership/auth/catalog files to simulate the outage.
   A last verified ratings snapshot must obey every policy watermark already
   learned. A new exclusion can temporarily make network counts unavailable
   until its complete snapshot arrives; that is expected policy enforcement.
7. **Clean stop.** When empty, explicitly `stop`, then `network-stop` if started.
   Confirm upstream closure and no dedicated workers. Capture after-support,
   stopped ownership-review and final sync/ratings results. Uncertain shutdown
   requires retained state and a report rather than force-clear/reinstall.

Return room link, test times/timezone, before/active/after redacted support,
completed checklist, maps/commands tested, vote and sharing counters, errors and
brief usability feedback. Do not send invitation/token/OAuth files, authorization
URLs/codes, private player IDs, state directories or unredacted logs. Include safe
HTTP status/failure stage when available. The maintainer distinguishes real human
gameplay evidence from isolated qualification; neither substitutes for the other.
