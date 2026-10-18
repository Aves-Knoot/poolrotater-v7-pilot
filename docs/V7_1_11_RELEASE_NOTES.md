# PoolRotater v7.1.11

Player statistics are replicated as a complete local, indexed database on each participating host. Ordinary totals refresh around every 20 minutes; successful `.stats` replies use the familiar player-facing presentation. Privacy and consent checks run separately, with a short lease that prevents stale sharing policy from remaining usable during an outage.

Authenticated hub transfers no longer have an artificial rate cap when both sides negotiate support. Background hub/network/catalog/release services use nice 17 and a shared CPU ceiling of half one core per machine. osu! API pacing and player anti-abuse protections remain unchanged.

Old clients work with the new hub. New clients retain conservative upload pacing against an older hub. Legacy request, cache and receipt formats remain supported. Existing gameplay, credentials, catalogs, settings, defaults, enrollment, history, opt-outs and update policies are preserved.

This is a controller update. Opted-in running hosts apply it after every configured room is empty naturally; publication does not start deliberately stopped rooms or override paused update policies.

Validation: 173 affected tests passed against the frozen runtime, a broader 222-test related regression batch passed (overlapping the affected batch), root/service-user resource guards, a protected full-catalog upgrade, mixed-version HTTPS checks, complete replica import under the CPU limit, and standalone protected hub lifecycle checks. Whole-repository discovery, unchanged long gameplay suites and new actual-provider gameplay tests were not repeated.
