# Pending maintenance and scheduled room recreation

An automatic empty-room discovery refresh could replace a room while an operator
empty drain was waiting for players. The worker and root proof pinned the old
identity, so the worker correctly reported room_changed and the update held,
leaving already-drained siblings stopped.

The proposed next patch makes the periodic discovery refresh yield to pending
operator maintenance. The refresh deadline and configured interval stay intact;
normal scheduling resumes after cancellation or expiry. Gameplay remains available.
A room-ID change during referee event handling is rechecked before readiness is
published. Identity changes and uncertain notices stay fenced; no old notice,
match-boundary evidence or closure authority is transferred to a different room.

Focused offline checks passed: 98 maintenance/drain/recovery checks, five existing
refresh-admission checks and three protected mailbox replacement-sequencing checks.
The new code is NOT published, installed or queued. Full installed release
qualification and publication remain a later batch; these unit checks are not
claimed as acceptance of an existing held host's repair.

Already-installed predecessors can have a held pre-admission request that their
independent recovery channel is forbidden to complete. A new signed offer cannot
bypass that installed restriction. Use supported local exact-operation review and
cancellation, wait for maintenance cancellation acknowledgement, then a fresh empty
maintenance operation before the documented stopped upgrade. Keep stopped intent,
settings and ownership history; never fabricate admission or clear unknown rooms.
