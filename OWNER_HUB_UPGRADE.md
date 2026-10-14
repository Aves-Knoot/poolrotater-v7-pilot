# Preserve an existing dev7, dev8 or dev9 instance

Use the new private release archive in a fresh folder. Keep the existing `first`
instance and all its state. Record before-support and the current registered
runtime SHA256. The expected dev7 runtime is:

`a340b0b719081c958853ec7435d90112588a26d348d46de057ce5bb9f8591932`.

If already on the published dev8 owner-hub pilot, confirm that its registered
runtime is `04f3dcd4b059a9f445d3949faf6707b1a8c8c5142c51544dbd4fc84adac84501`
and use that value for `--upgrade-from-sha256` below. A version label alone is
insufficient; stop for review if the installed digest is different.

If upgrading the published dev9 configuration-compatibility release, its
registered runtime is `0b8f12a7b8e2dfc9a708f8fc331e08583bcabccaa1a1132adb971b7319b81481`.
Use that exact digest for `--upgrade-from-sha256`. The liveness repair keeps the
same package version; the runtime digest and separate release tag identify it.
It preserves the stopped instance and starts no service. Do not reset authorization,
reimport owner data or discard unresolved room ownership to install the repair.

Stop the bot cleanly with its existing protected `stop` entry; confirm upstream
room closure and no dedicated worker processes. If a network agent is installed,
stop it too. Both services must be disabled/stopped before upgrade. Unknown room
ownership must be reviewed and resolved without discarding its journal.

```sh
sudo /usr/bin/python3.12 -I -S install.py --instance first \
  --bundle runtime.zip --sha256 dc3d5fd5fd0c9790008e134539ab90924c62ddb7af5afdf52345dfc3661fb69b --live-pilot \
  --upgrade-from-sha256 a340b0b719081c958853ec7435d90112588a26d348d46de057ce5bb9f8591932
```

This accepts the exact reviewed dev7-to-integrated transition. It starts no
service and performs no ledger migration. Use the new printed `pilot_release`.
Run `status`, `support` and `ownership-review` with the protected admin entry.
Authorization/catalogs/recovery and one-room configuration must remain intact.
The dev9 source-compatibility repair preserves existing decimal-string Discord
IDs and supported historical native column order instead of resetting settings
or history to fit a new representation. Existing player-language choices and
offer suppression stay local and are retained. This tester upgrade neither
discovers nor imports the owner's production data; owner migration requires its
own private configuration/schema preflight and approved stopped snapshot.
If upgrade was interrupted, inspect its transaction and use install.py's explicit
`--upgrade-status` / `--upgrade-recover complete|abort --transaction-id ...` flow
with the same approved old/new digests. Retain evidence; never remove journals.

## Explicit catalog trust adoption

Existing dev8 operators who already adopted bootstrap `2892f9fda822c2aade05fe5b28f354a57623b66555f75f2fb09fd4c0101f7a74`
and CA `60a0dac90855a1b0006062e9820c2144ba2240fc097cd685a6ca0b6fcb445f39` keep that trust and verified cache. Skip replacement;
use the existing protected catalog checks/import with the same9445 origin if an
updated download is needed. Do not repeat the old-to-new replacement transaction.

The former pilot's signers were discarded. This release carries new independently
pinned catalog root and HTTPS CA bytes; old cached catalogs remain available until
the complete replacement is verified. Confirm the old installed bootstrap digest
locally before issuing the replacement. For the original pilot it is:

`60d2846968d842c9838679ee79536d5963c219e08273eccaca0fc374434b207d`.

```sh
sudo /usr/bin/python3.12 -I -S "$pilot_release/poolrotater_host/pilot_admin_entry.py" \
  --instance first catalog-trust-replace --origin https://91.107.237.182:9445/ \
  --trust-root bootstrap.json --trust-sha256 2892f9fda822c2aade05fe5b28f354a57623b66555f75f2fb09fd4c0101f7a74 \
  --ca-file ca.crt --ca-sha256 60a0dac90855a1b0006062e9820c2144ba2240fc097cd685a6ca0b6fcb445f39 \
  --previous-trust-sha256 60d2846968d842c9838679ee79536d5963c219e08273eccaca0fc374434b207d \
  --confirm-replacement
```

It authenticates all three full catalogs, retains cumulative exclusions and the
old store, and does not alter local votes/settings/OAuth. If interrupted, leave
both services stopped and use `catalog-trust-recover --transaction-id ...` with
the exact reported local pending transaction. A partial replacement blocks startup.
A failed download before publication leaves the old catalog store usable.

The current prepared Standard/DT counts are 105502/105129; native Mania4/Mania7
counts are 19334/4347. Mania companions include farm and conversion evidence.
Current central conversion candidates all fail existing independent key checks;
their zero admitted rows preserve the owner's behavior, not a missing download.

## Local ratings and voluntary hub enrollment

While services remain stopped, explicitly run `ratings-migrate` then
`ratings-status`. This preserves a whole-ledger backup and enables durable vote
capture; it does not enroll or upload. Existing eligible history can be seeded
only by separate `ratings-seed` and a maintainer historical-seed grant.

Send the maintainer only your installation UUID/generation, intended hosting
account ID and directory-sharing choice from NETWORK_OPERATOR_GUIDE.md. Configure
the independently pinned hub, consume the bound short-lived invitation and verify
`network-status`. Separately agree ratings-sharing mode with the maintainer, then
use RATINGS_OPERATOR_GUIDE.md's `ratings-configure` and explicit reader mode.
Never send OAuth credentials, client secrets or token files.

Once checks are clean, use SHARED_HUB_TEST.md. The network agent can be installed
and started explicitly; installation/enrollment never starts a gameplay room.
Owner four-room adoption is a separate qualified, approved procedure. Do not use
this tester upgrade to replace or import the owner's running v6 installation.
