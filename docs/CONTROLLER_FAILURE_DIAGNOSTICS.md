# Controller failure evidence — v7.3.2

The v7.2.12 and v7.3.1 community incidents ended after an internal `NetworkError`
escaped `supervisor.tick()`. The original validation code was reduced to
`service_failed`; uploaded logs do not identify which check raised it. This
exception type also represents local state/ownership validation and is not proof
of an upstream network outage. The referee SIGKILL was observed during later
shutdown and is not established as the cause.

This isolated change records failure evidence before fatal cleanup starts:

- Reviewed internal validation codes are retained by `failure_details`.
- The native loop identifies its current component: configuration, maintenance,
  bookings, managed rooms, workers, status, and other bounded stages.
- The private `state/controller-failure.json` file and structured operational log
  retain the code, component, exception type and up to twelve source coordinates.
- No exception prose, arguments, local variables, source text, credentials or
  filesystem paths outside the registered release are captured.
- A failed evidence write cannot suppress the original error or cleanup.
- Existing ownership, privacy, room creation and shutdown decisions are unchanged.

This is diagnostic hardening, not a demonstrated fix for either incident's
unknown failing check. Do not blanket-retry `NetworkError` or infer permission to
clear ownership. It requires a coordinated controller update through the normal
empty-room gate. Existing v7.3.1 artifacts remain immutable. Failed services with
running room intent may first require the supported managed start/admission
workflow; an ordinary stopped heartbeat alone cannot authorize installation.

Xikeon's returned inventory proves its registration and dependency environment
match v7.3.1, its previous update was cancelled before commit, all workers were
reaped and all four room slots have known clean closure. This excludes an
unfinished upgrade as the explanation for that incident. Strohm's protected
bootstrap refusal still requires the local diagnostic output before choosing a
repair; its healthy running rooms must not be interrupted for diagnosis.
