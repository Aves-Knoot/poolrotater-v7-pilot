# v7.1.12 — community room resilience and responsive commands

Optional player-profile and top-score read timeouts no longer terminate a room. The bot retains the confirmed roster and READY/playing state, then retries enrichment with bounded backoff. Authorization, unsafe process cleanup and uncertain room mutations retain their existing recovery requirements.

Live community installations now use the same pinned osu! notification socket as the owner, with existing HTTP history reconciliation and fallback. This removes the routine polling delay before a command is seen; provider pacing and flood guards remain enforced.

A cold `.stats <username>` request now stays pending and posts its answer automatically when the background cache is ready. Requests are bounded, canceled on departure/rejoin/room change, and recheck visibility before replying. Complete local statistics replicas and their separate privacy lease are retained.

Ordinary playlist entries retain existing freemod; DT-specific entries keep their intentional forced mods. Website settings call it Freemod. Community map artwork, length, settings and player counts use the existing shared cards. Actual freemod match starts can show a clearly labelled normal-speed estimate; optional player speed mods can differ.

The signed controller updater can repair an already-running host whose configured failed rooms are all positively closed and parked. It requires repeated fresh controller observations and the independent clean ownership journal. Deliberate close/maintenance, stale state, live rooms and uncertain creation/closure do not authorize that shortcut. Healthy hosts retain the normal empty-room/private-admission checks. Deliberately stopped services remain stopped.

Configuration, defaults, titles, catalogs, credentials, enrollment, ratings, personal history, sharing choices and update policy are preserved. This is a controller update from v7.1.11 with unchanged storage and dependencies.

Validation includes typed HTTPS/RPC failure replays, bounded deferred replies and privacy tests, closed-slot admission refusals, community presentation checks, and a real protected root/service-UID upgrade using complete prepared catalogs. See the qualification receipt for exact test results. Unchanged whole-repository suites and new actual-provider gameplay tests were not repeated.
