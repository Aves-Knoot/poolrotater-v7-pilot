# v7.3.3 R25: Taiko and ruleset-separated ratings

> Live rollout checkpoint: Xikeon automatically installed v7.3.3 and reports
> three running rooms, one deliberately stopped slot and no failure codes.
> Strohm registered v7.3.3 but held at configuration preparation before startup;
> a single guarded completion refused. Local read-only diagnostic output is
> required. The owner remains on v7.3.2 with healthy Catch/4K rooms, and further
> update checks temporarily paused. The L3 Taiko pilot is not yet launched.


R25 technical acceptance passed both normal installed automatic-upgrade paths:
H from v7.3.0 and fresh I from v7.3.2 (2 of 2), including native commit and
post-upgrade health. The runtime SHA-256 is
`45254e8a24bee3b4c86259a31528e3e06a11b54aa201afca54e63981f0e45a41`. A separate
B pre-admission controller-interruption case failed before candidate code could
be installed and is not accepted as recovered. Proceed with the proven normal
update path and disclose this legacy limitation. Use matching published release assets or the signed automatic updater.
Deployment and live gameplay acceptance are separate from these donor results.

## Taiko charts and selection

R25 adds `taiko-fixed` and `taiko-auto`, using official Taiko difficulty at normal speed with no mods. The prepared production catalog contains 37,635 Ranked/Approved charts: 37,447 native Taiko charts and 188 conversions with verified source identity and Taiko qualification. Other-status charts are excluded. Player score tables and research databases are not distributed.

Converted charts use Taiko difficulty calculated for their original source file. The selector never substitutes Standard stars or unsupported speed-mod variants. The mode uses variety selection, configured star and length limits, song spacing, and sparse-pool fallback. Additional subjective style labels are not claimed.

Automatic difficulty uses Taiko profile PP and 16 aggregate points derived from qualifying Taiko NM top-play exposure. Qualified scores covered 3,298 of 9,000 profiles. The research panel did not establish population convergence. This curve is an exposure heuristic, not a skill, playability, or enjoyment guarantee. If the profile or calibration is unavailable, the configured star range remains in effect.

## Ratings and catalogs

Taiko ratings and statistics use a separate ruleset identity. Existing Standard, Mania, and Catch records remain in their existing namespaces. Successful rating uploads remain independent of snapshot reconciliation; a player's shared total includes a vote only after the verified shared snapshot contains it. During an outage, acknowledgement remains honest and synchronization can resume later.

The catalog service receipt records six-mode publication, including Standard content `03928b8f8f5999bd7e0e7517007319abe4f4294e63f821479b547dffc3c04af9` and Taiko content `71ececd64c6b8f3a34deed1008e0f80054c2e6de129add8a327ee2f3d3288f49`. Both H and I R25 post-upgrade receipts verify these Taiko and latest full Standard catalog references. This does not prove deployment to production hosts.

## Update staging and recovery

Before a future controller update creates its durable operation or stages files, it checks that the verified candidate and its expanded contents fit while preserving the existing 5 GiB free-space reserve. If space is temporarily short, the candidate remains downloaded, no native update operation is created, and the normal updater retries the capacity check after about 60 seconds. Existing rooms keep running.

The read-only check uses the existing signed artifact, installation baseline, and operator update policy. It changes no trust pins or consent and grants no new update authority.

After an operation has been created, an uncertain native outcome is held for review; the updater does not treat an unknown mutation as a harmless retry. A retry is permitted only when native checks prove the current installation baseline is unchanged, the operation was not submitted, and every configured room is either a healthy identity-matched running worker or positively confirmed closed. If there is an older native job, it must be a separately bound terminal cancellation for the same parent installation, with closure/restoration complete and no commit or service-stop attempt. A stopped room counts as closed only with matching ownership evidence, no room ID or worker PID, no creation candidate, no pending operation, and no reservation; any retained generation and release identity must agree with the clean ownership journal. Any mismatch remains held for review.

R25 also includes fixes for managed-operation admission, update-helper
recovery, recovery timer quiescence, final-receipt validation, and protected
catalog-prewarm record reading. Focused offline integration checks cover these
changed paths; the installed acceptance below is separate evidence.

## Qualification status

H's v7.3.0 and I's v7.3.2 normal R25 automatic-upgrade paths passed the unchanged
acceptance validator. The retained evidence includes occupied-match and booking
protection, a valid waiting configuration change, interrupted-download resume,
automatic helper retry, native commit, preserved room intent/settings, catalog
references, and post-upgrade health. A separate B pre-admission controller
interruption failed before the new code was installed; it is retained and is not
claimed as recovered. The accepted normal paths do not turn that failure into a
supported automatic recovery.

Matching source/runtime assets are published on GitHub, and signed pilot
metadata45 offers the exact runtime to v7.3.0/v7.3.1/v7.3.2. The compatible hub,
website and actual community Discord bridge are deployed; six wiki pages have
normal verified revisions. Owner rollout is safely queued and occupied rooms
continue; client installation, the selected owner Lobby3 Taiko pilot and actual
gameplay remain unverified. H and I used synthetic Discord routing; the
production bridge connection is separate live evidence, and Taiko room-specific
presentation still needs the pilot. See
[`TAIKO_RATINGS_RELEASE_PLAN.md`](TAIKO_RATINGS_RELEASE_PLAN.md) for the
remaining release gates.

## Live rollout under review

On 2026-10-25 one live Loved/Standard host committed registration of the exact
release but held at configuration preparation before startup. Its config hashes
are unchanged. A guarded remote completion refused; the local preparation
records are needed to identify the cause. This is not accepted as successful
production rollout and is not the earlier pre-admission controller-fault case.
The owner update is deferred, with the active 4K worker preserved. Keep held
state intact and return the maintainer's read-only diagnostic output rather
than reinstalling, clearing ownership or repeating startup.
