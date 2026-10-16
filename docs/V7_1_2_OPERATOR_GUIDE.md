# Installing v7.1.2 safely

Use the installer asset attached to the v7.1.2 GitHub release, rather than the
GitHub source ZIP. Verify its release SHA-256, extract it into a new directory,
then run `sha256sum -c SHA256SUMS.txt`. Keep the existing instance and state.

This patch changes provider/controller and worker code. Healthy running hosts
with pilot updates enabled can receive it through the signed channel, waiting
for the existing all-configured-rooms-empty controller window. Worker-only
replacement does not install the provider-side repair. Explicit updater pauses
and opt-outs are retained.

## A stopped or failed host

The network agent and updater do not give the hub remote shell or room-start
authority. An updater may download while the bot is stopped; it does not start
rooms or override an ownership-recovery requirement. The operator must complete
the following local steps.

1. Capture redacted `support`, `ownership-review` and `update-status` using the
   currently registered protected administrator entry. Retain the instance state
   and the service journal around the incident. A fresh empty public directory
   is not proof that old rooms closed.
2. If any ownership is uncertain, follow the documented stopped recovery checks
   and return their result before retrying. Do not delete journals, supply an
   invented room ID, reinstall, or create another instance on the same account.
3. Once every prior room is confirmed closed and ownership is clean, stop updater
   polling with the existing entry's `update-stop`; stop the network agent with
   `network-stop`. Both affect this installation only. Keep a private local
   backup. The bot must already be stopped, with no remaining service processes.
4. Upgrade from the extracted kit, replacing INSTANCE and the two SHA-256 values
   with the verified instance/runtime identities:

   ```sh
   sudo /usr/bin/python3.12 -I -S install.py --instance INSTANCE --bundle runtime.zip --sha256 NEW_RUNTIME_SHA256 --live-pilot --upgrade-from-sha256 CURRENT_RUNTIME_SHA256
   ```

   Supported current runtimes are v7.1.1
   `d9bc7d722b8b9f20d0c16ad79503bd8e811c41653a3e745f01c95df8a29e9efe`
   and v7.1.0
   `8c0971747c77a22a992749a6171363d60eb4a3bdc5f87460c55f88d881a21d35`.
   Take NEW_RUNTIME_SHA256 from the release checksums. The installer preserves
   state and does not start rooms. If upgrade is interrupted, retain its receipt
   and use supported upgrade-status/recovery rather than rerunning blindly.
5. Use the **new protected entry printed by the installer** for `status`,
   `support` and `ownership-review`. Confirm the expected version, clean ownership,
   retained authorization, catalogs, titles, settings and enrollment. Do not
   configure OAuth or enroll again just because the runtime changed.
6. Run `start` once locally when the checks are clean, then `status`. Return the
   new room links and before/after support. If startup is uncertain, retain state
   and report before another attempt. Resume the network agent with `network-start`
   and restore the previous updater polling/paused intent using `update-start`
   only if polling was previously enabled. Preserve existing sharing choices.

## Feedback

Return the incident journal, process exit/signal/OOM details if present, stopped
ownership-review, before/after support, update status, room links and test times.
Redact credentials, player identities, account IDs and private pacing hashes.
Do not send token files. Check several completed matches and score processing;
leave healthy rooms running normally and report any later empty-room refresh.
The score-timeout repair and the unconfirmed refresh interruption are separate.

Hosts still on v7.0.x need the supported one-time v7.1.0 storage migration first.
All sharing defaults, opt-outs and public trust pins from v7.1.1 are retained.
