# Private booking portal UI validation

The UI has three isolated local qualification layers. `tests/bookings_dom_fixture.js` runs the shipped `app.js` against the actual `index.html` IDs with a small DOM fixture. `tests/test_booking_real_browser.py` drives the shipped portal in Chromium against the real `Portal` and `PortalServer` routes. `tests/test_booking_package.py` checks the packaged static assets, preview dependencies, disabled-by-default config and protected booking entry point.

The real-browser test uses invented owner/friend identities, a local simulated OAuth redirect, synthetic host observations and a self-signed loopback TLS certificate. The browser context ignores that test certificate; it still exercises a secure same-origin page, Secure/HttpOnly/SameSite session cookies, CSRF, and a foreign-Origin request rejected by the actual portal with no CORS allow header. It uses the portal's actual HTML, CSS, JavaScript, API and invite routes. The test checks public slot privacy before booking, while reserved and after logout; native number validation; keyboard activation and focus placement; accessible labels on rendered controls; claim status and the arrival countdown; queued, applying, live, next-map and failed settings receipts; retention of the prior settings after a failed receipt; a polled revision conflict with draft preservation and explicit reload; owner controller grant/revoke and denied controller role changes; invitation creation and its joining page; successful clipboard copy and the UI fallback for a denied clipboard write; responsive overflow at 360, 768 and 1280 pixels; and the closing state. It also checks that map-setting copy describes the current map and visible queue staying in place while future selections use the new filters. For inactivity status, it checks the host-reported no-gameplay countdown and five-minute warning, empty-room policy copy, the running-map grace estimate, hiding cached and server-reported deadlines after the 30-second freshness bound, retaining the same absolute deadline across refreshes, and waiting for a fresh host report after a displayed deadline elapses rather than claiming the room has closed.

Run the real-browser qualification from `work/validation/private-booking/source`:

```sh
nice -n 19 /usr/bin/python3 tests/run_private_booking_browser.py
```

The runner refuses to start without `bubblewrap`, `systemd-run` and the staged browser tools. It captures the ordinary test-isolation boundary with `scripts.test_isolation.anchor_fd`, verifies the read-only namespace receipt inside the runner and test fixture, and starts a dedicated `systemd-run --scope` with `MemoryMax=1G`, `MemorySwapMax=256M` and `CPUQuota=100%` under nice 19. Chromium's virtual address space is unrestricted; the cgroup caps actual memory. The bubblewrap child uses a new network namespace, UID/GID 1000, no capabilities, `--die-with-parent`, and read-only mounts for the selected app source, test harness, browser tools and isolation verifier. It does not mount `/root`, credentials, production state or unrelated workspace directories. The portal, invented OAuth redirect and browser run together on loopback inside that namespace. Chromium receives `--no-sandbox` only inside this verified namespace.

The runner uses Playwright Python 1.64.0 and the matching Chromium headless shell, Chrome for Testing 156.0.8078.4 (Playwright browser revision 1248), staged at `validation/development-tools/private-booking-browser`. Dependency and browser downloads happen outside the runtime sandbox with an empty temporary home; the runtime itself has no external network. To qualify an extracted candidate package with the same harness, set `PRIVATE_BOOKING_SOURCE` to its dedicated read-only source root under `work`, containing `poolrotater_host`:

```sh
PRIVATE_BOOKING_SOURCE=/path/to/extracted/source nice -n 19 /usr/bin/python3 tests/run_private_booking_browser.py
```

The exact-package real-browser receipt is **1 test passed** in 33.950 seconds against the extracted payload for runtime SHA-256 `fa15a041bb3ae7469ff668fc306f69b8b61e63c46a4e43dd447a32cbfe1cef92`. The extracted payload's `index.html`, `app.js` and `app.css` hashes match the corresponding members in that runtime archive. The pass exercised the actual portal routes and shipped UI with the local simulated OAuth provider; it did not use production identity or services. The test's server thread, browser context and browser process close on completion; the systemd scope and bubblewrap namespace end with the runner. Ordinary isolated pytest collection sees the browser test but skips it before importing Playwright or starting a server; that check reported **1 skipped**. The dedicated command above is the required browser pass. The run was Chromium-only; Firefox and WebKit were not qualified.

The settings inventory exposes only fields classified as live or next-map by the host. It does not expose a recreation-required field, so this browser scenario directly asserts the live and next-map receipts and their current-map/visible-queue effects; a recreation receipt is intentionally not a required UI outcome for the current allowlist. The UI retains a distinct message branch if a host reports that a new session is required, but no player setting currently exercises that branch.

Run the DOM fixture and package checks from the outer `work` directory so the existing wrapper supplies its ordinary isolated namespace:

```sh
nice -n 19 scripts/offline validation/development-tools/node-v22.23.3/node --test validation/private-booking/source/tests/bookings_dom_fixture.js
nice -n 19 scripts/offline python validation/private-booking/run_tests.py validation/private-booking/source/tests/test_booking_package.py
```

After the settings outcome copy changes, the DOM fixture reported **1 passed**. Package checks also passed within the final 110-case closeout receipt.

These checks do not certify Firefox, WebKit, assistive-technology or screen-reader behavior. The clipboard fallback test makes the browser API reject with `NotAllowedError`; the successful copy uses Chromium's granted clipboard permission. Host readiness, current map, visible queue, roster, activity estimates, permission receipts and cleanup are invented local observations. The inactivity panel shows the host's time estimate, not proof that cleanup completed; an elapsed estimate waits for a fresh host report, and only confirmed closure ends the booking. No real osu! account, OAuth provider, game room, production network or public booking service is used. The browser test stops at the portal's closing state and does not establish native room closure or slot reuse.
