# v7.1.19 — room refresh and connection recovery

Empty-room refresh no longer tears down a newly created room solely because an ordered player-roster event is still being delivered. The bot obtains a fresh JoinRoom snapshot and waits for ordered delivery before retrying that explicit local, pre-dispatch refusal. Unknown provider mutation outcomes are never replayed.

Referee reconnection now receives its full five-second wire budget after API pacing admission. Previously a cooldown could consume almost the entire deadline, causing another timeout and renewing the cooldown. Reconnect retries use bounded backoff and send one disconnect lifecycle event per outage. Typed pacing errors remain distinguishable in diagnostics.

This release also includes the previously qualified v7.1.18 work: authoritative roster reconciliation for player counts and vote quorums, a directory projection fix for the scoring phase, and startup readiness ordering that finishes the initial catalog audit before the older updater evaluates fresh worker status. Full map catalogs, storage formats, dependencies and API rate limits remain unchanged.

## Existing running hosts

Healthy opted-in installations on the exact published v7.1.16 baseline or the owner-local v7.1.17 baseline receive this through the signed pilot channel. No reinstall, OAuth sign-in, enrollment or manual ownership-clear is needed. This is a controller update: every configured room on that host must be empty for the existing maintenance gate. One empty room does not interrupt occupied sibling rooms; each host updates independently. Late joins reset admission. Configuration, credentials, catalogs, ratings, statistics, titles, enrollment, opt-outs and existing boot policy are retained.

The owner already runs these exact v7.1.19 bytes; publishing the release does not restart those rooms. Deliberately stopped, paused or uncertain installations retain their guards. Publication is not a command to force-start them. Retain state and report any held/uncertain outcome instead of repeatedly starting rooms.

## Validation

143 affected tests passed, including real TLS negotiation and upgrade after a 4.95-second admission delay, ordered roster recovery, uncertain-write non-replay, and native multiroom handling. The owner was restored with all three workers healthy, connected Discord and real-time chat, and a fresh website feed. Full repository suites were not rerun. Exact-artifact installed automatic-upgrade qualification is recorded separately with this release before promotion; no actual third-party account or room was used for donor tests.

## Downloads

Existing automatic updaters fetch the signed runtime directly. For a fresh supported Linux x86_64, CPython 3.12, systemd installation, download `poolrotater-7.1.19-live-pilot.zip`, verify its release checksum and follow its README. GitHub's source ZIP is not the installer. The separately attached runtime ZIP is the same immutable runtime contained in that kit. Existing incident repair launchers keep their original pins and should not be repurposed for unrelated states.

Runtime SHA-256: `20486e57c4bc9118761dfa5e0c83e1ce3cd360561c2fbe18b9554661354871f6`
