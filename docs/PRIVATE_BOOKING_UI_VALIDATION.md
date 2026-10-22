# Private booking portal UI validation

The portal has two local qualification layers. `tests/bookings_dom_fixture.js` runs the shipped `app.js` against the actual `index.html` element IDs using a small DOM fixture. It exercises reservation controls, numeric setup values, designated friend IDs, role grant/revoke buttons, boolean and style controls, AR/length filters, the DT selector, apply/cooldown state, revision conflicts, current-map and five-item queue rendering with fixed map links, safe text rendering, unavailable-state copy, and retrying an uncertain mutation with the same UUID.

This is component-level DOM qualification, not a real-browser run. The fixture models only the DOM methods and events used by `app.js`; it does not implement browser layout, focus traversal, native form validation, clipboard permissions, navigation, or assistive technology. It cannot establish responsive rendering, keyboard operation, or screen-reader behavior, and is not cross-browser or accessibility certification.

`tests/test_booking_package.py` assembles a fresh standalone hubkit, checks the three static assets and preview reader dependencies, imports the booking preview and native pool classes with `python -I -S`, and exercises the protected `booking-check` entry point. It verifies that local config defaults disabled without creating browser-session storage. Under the ordinary unprivileged test namespace, the root-owned installed bootstrap correctly refuses the user-owned temporary staging tree; a root-owned run also checks the successful disabled-config result. A separate case verifies that the optional `preview_catalog_root` comes from local configuration and that browser requests cannot supply a catalog path.

Run the focused checks from the outer `work` directory so `scripts/offline`
provides the required isolated namespace and credential-free environment:

```sh
nice -n 19 scripts/offline python validation/private-booking/run_tests.py validation/private-booking/source/tests/test_booking_package.py
nice -n 19 scripts/offline validation/development-tools/node-v22.23.3/node --test validation/private-booking/source/tests/bookings_dom_fixture.js
```

The DOM fixture uses Node's built-in test runner and downloads no packages. The package test imports and assembles its staged runtime using the Python standard library; its `python -I -S` subprocess also qualifies the installed closure without site packages.
