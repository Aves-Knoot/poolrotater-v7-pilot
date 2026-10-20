# v7.2.1 — Catch setup and retained rollback

Catch additions now reuse the unused lobby's existing **public Discord status
channel**, as well as its control channel. A missing or colliding route is
refused before committing configuration. Existing rooms keep their panels,
permissions, shared Discord identity and greetings.

A completed Catch configuration transaction remains usable after a controller
upgrade only through the exact, root-protected committed upgrade history. Its
original runtime digest remains provenance. Pending, foreign, unfinished or
unverifiable transactions are still refused. Configuration rollback retains
Catch opinions/history and current preferences in the original rooms.

Gameplay, selection, the full catalogs, dependency wheels, supervisor reliability
code and legacy statistics/ratings readers are byte-identical to v7.2.0. Catch
remains optional: native NM, fixed initial 3.00–4.00 stars, Ranked/Approved only.
Loved, conversions, styles, farm weighting and automatic difficulty stay outside
this pilot. Upgrading does not add a room to another host.

The direct reviewed predecessors are exact v7.1.20 and v7.2.0. This is a shared
controller update and uses the existing empty-room maintenance gate. Occupied
matches are preserved. Older hosts must complete their published upgrade chain
first. Keep installation state, authorization, catalogs, enrollment and choices.

Use `NATIVE_CATCH_OPERATOR_GUIDE.md` for fresh installation or explicit Catch
addition. Preparation takes reviewed JSON through
`catch-pilot-prepare --settings-stdin`. The included normalization helper is
only for stopped, positively closed diagnostic rows; it refuses unknown or
active ownership and creates no rooms. Keep state if any outcome is uncertain.

The owner Catch pilot first started on v7.2.0 after correcting its retained
Discord route. Its website map, queue, settings and artwork and its Discord
connection were verified. That operational startup does not establish gameplay
acceptance. See `VALIDATION.json` and separate live-startup evidence for the
exact tested artifact, donor cases, remaining fixture limits and player tests.

Linux x86_64/glibc, CPython 3.12 and systemd remain the qualified installer target.
This release does not claim ARM or additional operating-system qualification.

An existing independently pinned recovery agent must also be Catch-capable before
Catch recovery polling resumes. Retained pins and opt-outs are preserved; see the
operator guide for this separate reviewed step. Fresh recovery setup uses current
code. The owner pilot is handled through its own reviewed pin refresh.
