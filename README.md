# Current release: v7.1.20

Use the [v7.1.20 installer release](https://github.com/Aves-Knoot/poolrotater-v7-pilot/releases/tag/v7.1.20) and [release notes](docs/V7_1_20_RELEASE_NOTES.md). Room handoffs retry definite pre-mutation refusals safely, and player greetings use resolved names before optional score lookups finish. This controller release waits for all configured rooms on each host to empty once. Existing settings, authorization, enrollment, data and opt-outs remain.

# PoolRotater v7.1.19

Use the [v7.1.19 release](https://github.com/Aves-Knoot/poolrotater-v7-pilot/releases/tag/v7.1.19) and [release notes](docs/V7_1_19_RELEASE_NOTES.md). Existing opted-in v7.1.16/v7.1.17 installations update automatically after all their own rooms are empty. Keep your existing settings, OAuth, catalogs, history, ratings and enrollment. This controller update preserves occupied rooms while waiting. The owner already runs these exact bytes.

Fresh hosts should download the installer asset and follow its README; GitHub source ZIPs are not installers. Retain instance state and use supported recovery if ownership is uncertain. This release repairs empty-refresh roster races, reconnection cooldown starvation and initial status readiness, and reconciles stale player counts and vote quorums.
