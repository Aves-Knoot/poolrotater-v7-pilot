# Upgrade an existing instance to dev10 without changing its room count

Keep the existing instance and all of its state. The fresh two-room instructions
in TWO_ROOM_OPERATOR_HANDOFF.md are a separate route: an ordinary upgrade cannot
add a second room. Do not reinstall, copy tokens or reset ownership journals.

This release includes the October14 chat-pacing and fatal-shutdown repair. For
Xikeon's existing `two`, preserve the selected PoolRotator titles, completed
OAuth, catalogs, hub enrollment and contribution permission. Upgrade in place;
do not create another instance, request another invite or migrate ratings again.

## Record and stop the existing instance

Using its current protected entry, capture status/support and the registered
runtime SHA256. Stop the bot cleanly, positively confirm actual upstream room
closure and no bot workers, then network-stop if its agent is installed. Stop
the network agent before stopped ownership-review: it shares the dedicated UID
and must not keep that review's process-exclusion guard busy.
Both units must be disabled/stopped. Resolve any ownership uncertainty through
the documented review/recovery flow before attempting an upgrade.

This artifact contains exact reviewed transitions from:

- Published dev9 difficulty-floor runtime:
  `b014b75a68fe9adb1612700f74e0038e3d689a7e71debde59518d41a469f10e1`.
- Published dev7 chat-access repair:
  `a340b0b719081c958853ec7435d90112588a26d348d46de057ce5bb9f8591932`.

A donor-only historical qualification origin is not an operator release. Other
digests require explicit compatibility review: do not substitute a listed digest
for the runtime actually installed. The label dev9 alone is not an exact identity.

## Install the verified new archive while stopped

Download the dev10 release archive, independently verify its published SHA256,
extract to a new folder and check SHA256SUMS.txt. For an existing `first` instance
on the published difficulty-floor runtime:

```sh
sudo /usr/bin/python3.12 -I -S install.py --instance first \
  --bundle runtime.zip --sha256 199e4e228e833d6ce051d76b47817e558f261d0224d2cd9c88e3d7eadf0b7b81 --live-pilot \
  --upgrade-from-sha256 b014b75a68fe9adb1612700f74e0038e3d689a7e71debde59518d41a469f10e1
```

Use the actual old digest instead if it is the reviewed dev7 origin. Do not add
`--preset`; that is refused on upgrades. The command preserves state and starts
no services. Use the exact new release path it prints for subsequent protected
administration. Capture status/support/ownership-review and room-settings-status.
Existing authorization, catalogs, preferences, ratings and clean recovery remain.

Interrupted upgrades use the installer's explicit upgrade-status/upgrade-recover
complete|abort flow with the same approved old/new digests and transaction ID.
Retain evidence; do not delete journals or rerun a different installation.

## Catalog and optional shared-vote checks

Existing installations with bootstrap 2892f9fda822c2aade05fe5b28f354a57623b66555f75f2fb09fd4c0101f7a74 and public CA
60a0dac90855a1b0006062e9820c2144ba2240fc097cd685a6ca0b6fcb445f39 keep their catalog trust and cache. Do not replace them merely
because the code was upgraded. Existing enrollment/sharing/reader choices also
remain; no new invitation or grant is needed for an unchanged active binding.

The old dev7 pilot trust requires explicit replacement before downloading from
the current https://91.107.237.182:9445/. Confirm the old installed trust digest first.
For the original pilot it was
`60d2846968d842c9838679ee79536d5963c219e08273eccaca0fc374434b207d`.
Use the new protected entry's catalog-trust-replace with the new pinned kit
bootstrap/CA, the actual previous digest and confirm-replacement. A partial
replacement blocks startup; use its documented catalog-trust-recover transaction
instead of deleting state. A verified existing catalog may remain usable during
the stopped review. All imported modes are full prepared catalogs, not samples.

If durable rating capture was never prepared, run ratings-migrate while stopped.
Do not seed history for the initial pilot. Directory sharing and vote sharing
require separate choices; central contribute permission must precede local
ratings-configure. A downloaded snapshot is separate from upload acknowledgments
and does not implicitly enable network weighting.

New title/gameplay patches use room-settings-configure --settings-stdin while
both units are disabled/stopped. It preserves unrelated preferences and changes
no account, room alias, preset, storage path or topology. Room-settings-status is
safe while running but describes saved defaults.

Start once only after clean checks and during an agreed test window. Return the
room links and safe before/active/after support. Retain state on uncertain startup
or shutdown. Use TWO_ROOM_TEST.md for a fresh two-room installation or
SHARED_HUB_TEST.md for the retained one-room instance. Human gameplay remains
separate from the donor's simulated technical acceptance.
