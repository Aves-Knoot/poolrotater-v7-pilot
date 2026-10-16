# v7.1 community command audit

This audit covers retained native player commands when optional community-host
integrations are absent, and command input that could previously terminate a
worker or change selection state unexpectedly. Production rooms, OAuth and real
provider requests were excluded. Tests run through `scripts/offline`, which
sets nice 15 and removes host networking and credentials.

## Repairs

* `.discord` always has an explicit-command fallback to the public community
  invite `https://discord.gg/KmNhHbDEbv`. An operator's nonempty custom invite
  takes precedence. An intentionally blank configured invite continues to
  disable automatic Discord reminders. `say()` discards empty or whitespace
  content before dispatch, including an empty localized result.
* Blank command dispatches return before indexing the first token.
* Standard `.variety` rejects NaN/infinite values before the map pool setter.
  Ordinary percentage/range checks previously compared NaN without rejecting
  it. Existing valid percentages and no-argument defaults are retained.
* Staff `.range` rejects nonfinite, negative, equal or reversed endpoints before
  calling the selection/mutation helper. Existing valid range behavior stays.
* `.help2` divides the complete public list at word boundaries into messages of
  at most 380 characters, preserving the existing 400-character `say` limit
  and its ordered bounded backpressure queue. The final commands are no longer
  silently truncated, particularly in Mania. Short `.help` remains one line.
* Disabled/missing optional AI replies directly with a throttled unavailable
  message before constructing live room context. The disabled AI drain would
  previously discard an unavailable reply placed in its model outbox. Empty
  `.ai` usage also replies directly, so it survives a disabled model outbox.
* A missing PP projection integration returns an unavailable message. Existing
  disabled-calculator and not-yet-ready results retain their normal replies.
* `.hidden`/`.show` now guard the saved-name lookup together with the existing
  visibility write. An unavailable local statistics store returns a failure
  reply and does not claim that privacy changed. Successful privacy handling
  and the separate community statistics reader remain intact.
* `.help length` correctly describes the existing maximum-length alias.

No command-wide exception catcher was added. Provider uncertainty and ownership
errors still propagate to their existing reconciliation and supervision paths.
There is no retry of ambiguous writes. Hidden novelty commands retain their
existing public-player, mode, playlist-audit, match-state and cooldown gates.

## Evidence

`tests/test_community_commands.py` contains 17 tests using the real `LobbyBot`
constructor, native command handler, localization, local stores and `say`
delivery through simulated provider/referee boundaries. The existing Standard
and native Mania fixtures use test-owned data, not the owner configuration.

Coverage includes:

* Every public registry member except `.stats`: 52 commands in each of Standard
  and native Mania, after construction and during a simulated playing state.
  `.stats` is deliberately covered by the independent v7.1 statistics tests.
* All 36 ordinary command-help entries and 16 help aliases through both `.help`
  and `.commands`, with `.` and `!` prefixes: 208 actual handler invocations.
* Full help through the actual 400-character provider boundary, both prefixes
  and both rulesets; no novelty command exposure; player/moderator/admin views.
* Malformed PP requests, source-command arguments, NaN/infinite percentages and
  staff ranges. Invalid inputs do not reach pool/mutation setters.
* Missing AI/PP/Discord configuration, throttled AI-unavailable replies and
  missing/erroring privacy storage.
* Real role checks denying player access to staff mutation and Discord-only
  reports, replay/skip alias forwarding, Mania style aliases and restrictions,
  and novelty refusal for spectators or Mania.
* An actual provider uncertainty exception remains uncaught by the command
  boundary, preserving its recovery meaning.

The integration milestone ran **47 tests, all passing**: the 16 new command
tests, 6 Discord fallback tests, 7 chat backpressure tests, current-map PP
display tests and targeted existing Mania help/privacy and command-flood tests.
Runtime was approximately 0.6 seconds in the offline sandbox. Expected injected
store failures and bounded-backpressure warnings are test evidence, not live
errors. One subsequent targeted empty-AI usage test passed after making usage
reply directly instead of entering the disabled model outbox. No large
validation artifacts were generated.

## Limits and remaining qualification

These tests prove parsing, routing, response bounds and failure behavior at the
simulated provider boundary. They do not establish osu! wire acceptance, live
playability of every command/state combination, network timing, real Discord
availability or a complete distributed `.stats` rollout. The native Mania
fixture is 4K; 7K shares the command ruleset, but full-catalog donor acceptance
must additionally verify its actual installation and preserved key settings.
Shared command mutations are simulated; no occupied room was used for testing.

The public-registry sweep uses no-argument commands in one playing-state
scenario. It is not an exhaustive Cartesian product of every possible command,
argument, room state and permission. Existing dedicated replay/novelty, playlist,
moderation, language, voting and lifecycle suites continue to provide their
specific coverage; unchanged complete suites were not repeated for this narrow
audit. Full-catalog installed upgrade qualification belongs to the integrated
v7.1 release acceptance, not these small command-unit fixtures.
