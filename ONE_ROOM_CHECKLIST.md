# Consenting one-room observation

Use after release review, operator consent and successful local authorization.
Agree a test window with the owner and other participating players. This is a
live test checklist, not evidence that these observations have already passed.

1. Save the redacted `support` output before start. Confirm exact release/preset,
   expected account locally, imported catalog modes and no recovery pending.
2. Start once. Confirm exactly one new room, correct account/preset/star range,
   eligible maps and exclusion of beatmap 1549496. Note timestamp and timezone.
3. Have consenting other players join, download in their clients and play a few
   maps. Test help/queue/skip/votes and the configured language. The bot downloads
   prepared DBs, never beatmap/audio files. Its referee account is not a player.
4. With at least two players, check countdown/readiness and an AFK/unready player
   without holding the room hostage. Confirm `.start` requires someone ready.
5. Observe chat pacing, map/song spacing, normal joins/leaves and match completion.
   Record concise positive/negative/bug notes without copying private chat.
6. Let the room become empty and stop gracefully. Confirm no workers/referee child
   or room remains, then capture `support` again. Test restart only after confirmed
   clean closure; do not intentionally inject live crash/unknown mutations in the
   initial pilot without separately agreed consent.

Return OS/Python/systemd/virtualization details, kit/runtime digests, installation
and callback usability notes, before/after `support`, observation length and steps
actually exercised. Redact account identifiers, authorization URLs, client secrets,
codes, tokens and private room/chat details. If startup or shutdown is uncertain,
retain private state and report the phase; never clear a journal or recreate blindly.
