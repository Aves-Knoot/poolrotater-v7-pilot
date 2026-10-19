# v7.1.20 — room update retries and player names

Queued room updates now survive an explicit worker refusal to prepare while the room is still healthy. The controller retains the target, disposes only the unused candidate, and retries after a fresh continuous empty interval with bounded backoff. A late player arriving during preparation also postpones the update before any room mutation. Cancellation remains effective. Unknown API outcomes, failed cleanup, ownership checks and storage guards retain their existing review behavior.

Join greetings use locally known player names immediately. Background profile results supply the name before the optional score lookup finishes; late results for a departed player or an old room are ignored. Real numeric usernames remain valid. Welcome and custom greetings are sent once per join when the name becomes available.

This is a controller release: all configured rooms on each host must be empty together once. No forced player removal or occupied-room restart is required. Subsequent compatible worker-only updates still roll one empty room at a time. Existing room defaults, OAuth, Discord, catalogs, enrollment, sharing choices, statistics, ratings and boot preferences are preserved. No database migration or dependency change is included.

Healthy opted-in exact v7.1.16 and v7.1.19 hosts are admitted by the signed pilot feed. A pending older operation is retained; hosts may finish v7.1.19 before taking this release. Keep the existing installation and state; no reinstall, reauthorization or repeated ownership-clear is required. Stopped or recovery-required installations still need their documented checks.

Runtime SHA-256: `b88e3c2a80ca94a68ee4a3412632c26d7e039d1d6c6e198f2becc397980da595`

See VALIDATION.json for the completed automated checks and their limits. Disposable provider fixtures use full catalogs; actual osu! gameplay is not part of offline qualification. Linux x86_64/glibc, CPython 3.12 and systemd remain the supported installer target. ARM support is unchanged.
