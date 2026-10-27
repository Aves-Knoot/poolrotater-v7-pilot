# Retained maintenance evidence in v7.3.7

This release preserves the completed or cancelled controller-update record
before a later update replaces it. The retained record is evidence only: it
does not grant permission to close a room, clear ownership, reset settings or
replay a provider request.

When the retained record, exact committed native upgrade, installation binding
and fresh controller observations all agree, the updater can retire an obsolete
automatic maintenance request. A separate private decision retains the original
request and receipt bytes. It never changes an unfinished drain into a claimed
closure. The next drain still needs its own request, gate, nonce and actual
empty-room acknowledgements. Interrupted retirement reuses the same decision.

Existing room settings, deliberately stopped slots, enrollment, authorization,
sharing preferences and player databases remain in place. Database and
dependency contracts are unchanged. Catch keeps its own preferred-difficulty
scope; Standard, Taiko, Mania 4K and Mania 7K retain their separate scopes. Old
mixed Standard/Catch preferences are preserved without guessing their origin.

## First transitions and recovery limits

An older updater cannot create the new history reference before it has been
replaced. Missing historical evidence is not reconstructed from version labels,
room lists or logs. Existing guarded all-empty admission remains available when
its independent checks qualify; an uncertain maintenance outcome or creation
still requires the supported review procedure.

A failed room-creation request can have server-side effects. A generic referee
rejection and an empty non-exhaustive room listing do not prove that no room was
created. Preserve the installation and report the ownership review and any
candidate room ID. Do not reinstall, delete a journal or retry creation to get
past a hold. This release does not yet isolate every uncertain room failure from
its healthy siblings; the October 27 refresh incidents remain a separate issue.

## Qualification status

The exact unpublished candidate passed the affected subsystem regression and
four isolated stopped-upgrade fixtures for v7.3.4, v7.3.5 and v7.3.6, including
preservation of unknown creation. A full six-mode installed donor upgrade from
v7.3.6 passed through an explicit verified candidate SDK/native queue recovery,
with occupied-room and booking protection, independent sibling draining,
configuration preservation, interrupted-helper retry, recovery-pin refresh and
teardown. Earlier failed QA observers are retained and excluded from acceptance.

A separate fresh installation of the exact v7.3.6 runtime also passed its
unchanged automatic updater path: interrupted download and range resume,
automatic queueing, occupied-match protection, booking cleanup, independent
empty-sibling drain, a waiting settings edit, interrupted-helper retry, native
commit, preserved configuration/data, reserved-slot intent and recovery-pin
refresh. No manual download, candidate SDK apply or rescue command submitted
that update. The original pin observer refused before mutation because the
recovery timer was active; a distinct observer passed after only that test timer
was paused and its running helper finished. Both cases passed teardown and an
independent check for known closure and zero remaining instance processes.

The affected subsystem run covers 319 distinct cases. Six initial test-fixture
failures were resolved by correcting the timer-I/O fixture boundary and restoring
an exact historical runtime fixture; only the affected modules were rerun.
Actual installed drain cancellation/redrain rounds, the complete repository
suite and real-account/gameplay tests were not executed for this candidate.
The automatic test begins with a fresh v7.3.6 installation; it does not recreate
missing history on older installations or discharge their existing holds.

The matching release assets contain the exact validation scope and checksums.
Availability in GitHub or the signed feed remains separate from installation on
any particular host. Inspect actual registration, room health and update status
rather than interpreting an offer or completed download as installation.
