# v7.1.16 — repair a stopped v7.1.13 startup failure

This is a local, explicitly authorized repair for an existing operator instance
whose signed v7.1.12 -> v7.1.13 update committed, then failed before room startup.
It retains the same installation, authorization, catalogs, titles, tuning,
ratings, statistics, enrollment and sharing/update preferences. No reinstall,
OAuth sign-in or repeated ownership-clear is needed.

The runtime includes the narrow cached-configuration compatibility fix first
qualified in owner-local v7.1.15: the new managed background-player-profile
boolean can be added without erasing local settings or accepting arbitrary
schema changes. Full catalogs, dependencies, storage formats and provider
pacing are unchanged.

The pinned command verifies the existing failed transaction, original signed
approval, complete code inventories, unchanged configuration, stopped processes
and positively closed ownership. It archives the old job and creates an explicit
stopped repair transaction. Ordinary protected native upgrade/recovery performs
replacement, fresh provider checks and one startup attempt. Successful healthy
workers are required before the old scheduler intent is retired with evidence.
Interrupted/uncertain startup never grants another automatic attempt.

The independent recovery pin and its policy are retained. Existing enabled or
disabled service choices are preserved; this does not grant boot autostart.
Original network-running intent is restored by the native transition. The
--resume-agents flag resumes previously active update polling after the repair.

This package is for the stopped committed-failure state, not an occupied-room
update, a fresh installer, or a general recovery override. The owner instance is
explicitly excluded. Existing release assets and queued owner runtime bytes
remain unchanged. Release registration does not advance the global channel head.

The same command returns already_running without another start after a
successful repair. A refusal retains root-private reports and all state. Do not
repeat startup when an outcome is uncertain. Return the final JSON; report files
may contain private account identifiers and must be redacted before forwarding.

Actual provider/gameplay acceptance remains the operator's next observation.
