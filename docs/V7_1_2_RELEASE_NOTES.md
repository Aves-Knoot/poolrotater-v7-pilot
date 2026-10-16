# v7.1.2 — score-read resilience

A temporary timeout while fetching scores after a match could terminate a room
worker. Match-score reads now tolerate bounded transport deadlines, HTTP 429 and
server errors, retrying with backoff within the existing scoring window. The
normal rotation timeout remains; failed reads never create scores or PP gains.
Provider cooldown, request accounting and uncertainty are retained. Authority
errors, unsafe child cleanup and uncertain RPC results still require review.

Worker exit diagnostics now include the exit code and RPC/failure-report state.
Closure-recovery errors expose an allowlisted failure code without private
exception text. This improves diagnosis when a worker cannot flush a traceback.

This release does **not** claim to fix the separate October 16 empty-room refresh
interruption: its root cause still needs the affected operator's stopped support,
ownership review and service journal.

Existing settings, OAuth, catalogs, enrollment, sharing choices, votes and history
are preserved. No database or dependency migration is included. This is a
controller update: enabled automatic updates wait for all configured rooms to be
healthy, empty and idle under the existing admission gates. A stopped or failed
installation may download an update, but the timer never starts rooms for it.
Use the supported local stopped upgrade after clean ownership is confirmed.

Validation includes 108 affected provider/RPC/scoring/lifecycle tests, 21 upgrade
checks and 22 updater checks; exact protected root/service-UID stopped upgrades
from both v7.1.0 and v7.1.1 preserve the full Standard/4K/7K catalogs, credentials,
configuration, explicit opt-outs and SQLite history. No actual osu! room test,
new gameplay soak or unchanged whole-repository suite was run for publication.

See V7_1_2_OPERATOR_GUIDE.md before restarting a failed installation.
