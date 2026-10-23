"""Real Chromium qualification against the shipped portal routes and UI."""
import json
import re
import os
import ssl
import subprocess
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from poolrotater_host.bookings.repository import Repository
from poolrotater_host.bookings import server as portal_server
from poolrotater_host.bookings.server import Handler, PortalServer
from poolrotater_host.bookings.web import Portal


HOST = str(uuid.UUID(int=501))
INSTALLATION = str(uuid.UUID(int=502))
GENERATION = str(uuid.UUID(int=503))
OWNER = 707
FRIEND = 808
SITE = "https://poolrotater.test:8443"


class Clock:
    def __init__(self):
        self.offset = 0

    def __call__(self):
        return time.time() + self.offset

    def advance(self, seconds):
        self.offset += seconds


class SimulatedOAuth:
    def __init__(self):
        self.next_actor = "owner"

    def authorization_url(self, state):
        actor, self.next_actor = self.next_actor, "owner"
        return f"{SITE}/test-provider/authorize?state={state}&actor={actor}"

    def exchange(self, code):
        users = {
            "invented-owner-code": {"id": OWNER, "username": "Invented Browser Owner"},
            "invented-friend-code": {"id": FRIEND, "username": "Invented Browser Friend"},
        }
        if code not in users:
            raise ValueError("invented OAuth code refused")
        return users[code]


class BrowserHandler(Handler):
    def do_GET(self):
        parsed = urlsplit(self.path)
        if parsed.path == "/test-provider/authorize":
            query = parse_qs(parsed.query)
            if set(query) != {"state", "actor"} or len(query["state"]) != 1 or query["actor"] not in (["owner"], ["friend"]):
                self.send_error(400)
                return
            target = "/private-lobbies/oauth/callback?state=" + query["state"][0]
            target += "&code=invented-" + query["actor"][0] + "-code"
            self.send_response(302)
            self.send_header("Location", target)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.dispatch("GET")

    def dispatch(self, method):
        portal = self.server.portal
        original = portal.handle

        def with_synthetic_host(method_name, target, headers, raw=b""):
            result = original(method_name, target, headers, raw)
            self.server.requests.append({"method": method_name, "path": urlsplit(target).path,
                                         "origin": headers.get("Origin"), "status": result.status,
                                         "headers": dict(result.headers)})
            if method_name == "POST" and urlsplit(target).path == "/private-lobbies/api/reserve" and result.status == 201:
                self.server.make_ready(json.loads(result.body)["booking"])
            return result

        # Reuse Handler.dispatch unchanged for request parsing and response security.
        portal.handle = with_synthetic_host
        try:
            super().dispatch(method)
        finally:
            portal.handle = original


class BrowserPortalServer(PortalServer):
    def __init__(self, address, portal, repository, cert, key, clock):
        original_handler = portal_server.Handler
        portal_server.Handler = BrowserHandler
        try:
            super().__init__(address, portal, fixture=True)
        finally:
            portal_server.Handler = original_handler
        self.repository = repository
        self.clock = clock
        self.sequences = {}
        self.requests = []
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(certfile=cert, keyfile=key)
        self.tls = context

    def get_request(self):
        sock, address = super().get_request()
        return self.tls.wrap_socket(sock, server_side=True), address

    def make_ready(self, booking):
        lease, = self.repository.leases(HOST, INSTALLATION, GENERATION)
        self.sequences[booking["id"]] = 1
        self._observe(booking, lease, roster=[], sequence=1, phase="awaiting_claim")

    def set_roster(self, booking, roster, *, receipt=None, effective_settings=None, activity=None):
        lease, = self.repository.leases(HOST, INSTALLATION, GENERATION)
        current = self.repository.view(booking["id"], OWNER)
        sequence = self.sequences[booking["id"]] + 1
        self.sequences[booking["id"]] = sequence
        observation = {
            "sequence": sequence,
            "phase": "active", "room_id": 998877, "incarnation": "invented-room-incarnation",
            "roster": roster, "roster_known": True, "updated_at": self.clock(), "ready": True,
            "closed": False, "effective_settings": effective_settings or current["settings"], "cleanup_complete": False,
            "current_map": {"beatmap_id": 9871, "label": "Invented current map", "stars": 4.2, "hit_length": 180},
            "queue": [{"beatmap_id": 9872, "label": "Invented queued map", "stars": 4.4, "hit_length": 205}],
            "queue_known": True,
            "activity": activity if activity is not None else current["activity"],
            "ownership_known": True, "operation_receipts": [receipt] if receipt else [],
            "installation_id": INSTALLATION, "generation": GENERATION,
            "session_generation": lease["session_generation"], "alias": "room1",
        }
        self.repository.observe(HOST, booking["id"], observation)

    def _observe(self, booking, lease, *, roster, sequence, phase):
        self.repository.observe(HOST, booking["id"], {
            "sequence": sequence, "phase": phase, "room_id": 998877,
            "incarnation": "invented-room-incarnation", "roster": roster,
            "roster_known": True, "updated_at": self.clock(), "ready": True, "closed": False,
            "effective_settings": booking["settings"], "cleanup_complete": False,
            "ownership_known": True, "operation_receipts": [],
            "installation_id": INSTALLATION, "generation": GENERATION,
            "session_generation": lease["session_generation"], "alias": "room1",
        })


class RealBrowserQualification(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if os.environ.get("PRIVATE_BOOKING_BROWSER_SANDBOX") != "1":
            raise unittest.SkipTest("use tests/run_private_booking_browser.py for isolated Chromium qualification")
        from test_isolation import require_test_isolation
        require_test_isolation("ordinary")
        cls.temp = tempfile.TemporaryDirectory(prefix="private-booking-browser-")
        cls.root = Path(cls.temp.name) / "private"
        cls.root.mkdir(mode=0o700)
        cert = Path(cls.temp.name) / "invented-test-cert.pem"
        key = Path(cls.temp.name) / "invented-test-key.pem"
        subprocess.run([
            "/usr/bin/openssl", "req", "-config", "/dev/null", "-x509", "-newkey", "rsa:2048", "-nodes",
            "-keyout", str(key), "-out", str(cert), "-days", "1", "-subj", "/CN=poolrotater.test",
        ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        cls.clock = Clock()

        def eligibility(host_id, alias):
            return {"host_id": HOST, "installation_id": INSTALLATION, "generation": GENERATION,
                    "fresh": True, "supported": True, "effective_limit": 4, "capacity_used": 0,
                    "observed_booking_ids": [], "supported_presets": ["standard-auto"]}

        cls.repository = Repository(cls.root, clock=cls.clock, eligibility=eligibility)
        cls.repository.configure_slot("west", HOST, "room1", "West private pool",
                                      ["standard-auto"], {"sr_min": 1, "sr_max": 5}, enabled=True)
        cls.portal = Portal(cls.root, cls.repository, origin=SITE, oauth=SimulatedOAuth(),
                            enabled=True, clock=cls.clock)
        cls.server = BrowserPortalServer(("127.0.0.1", 8443), cls.portal, cls.repository,
                                         cert, key, cls.clock)
        cls.thread = threading.Thread(target=cls.server.serve_forever,
                                      kwargs={"poll_interval": 0.02}, daemon=True)
        cls.thread.start()
        cls.booking = None

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(3)
        cls.temp.cleanup()

    def test_real_browser_booking_lifecycle_and_stale_draft_protection(self):
        from playwright.sync_api import expect, sync_playwright
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                headless=True,
                executable_path=os.environ["PRIVATE_BOOKING_CHROMIUM"],
                args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-background-networking",
                      "--disable-component-update", "--disable-sync", "--no-first-run",
                      "--no-default-browser-check", "--host-resolver-rules=MAP poolrotater.test 127.0.0.1"],
            )
            context = browser.new_context(viewport={"width": 360, "height": 800}, ignore_https_errors=True)
            page = context.new_page()
            page.add_init_script("""(() => {
              const baseNow = Date.now.bind(Date);
              window.__bookingNowOffset = 0;
              Date.now = () => baseNow() + window.__bookingNowOffset;
            })()""")
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(SITE + "/private-lobbies/", wait_until="networkidle")
            expect(page.get_by_role("heading", name="Private lobbies")).to_be_visible()
            self.assertEqual(page.locator("#page-message").inner_text(), "")
            self.assertEqual(page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), True)

            # The unauthenticated public projection contains only room availability.
            public = page.evaluate("fetch('/private-lobbies/api/slots').then((r) => r.text())")
            for private_value in ("password", "room_id", "players", "join_url", str(OWNER)):
                self.assertNotIn(private_value, public)
            self.assertNotIn('"activity"', public)
            self.assertNotIn('"activity_updated_at"', public)

            page.keyboard.press("Tab")
            self.assertEqual(page.evaluate("document.activeElement.id"), "sign-in")
            page.keyboard.press("Enter")
            expect(page.get_by_role("button", name="Sign out")).to_be_visible(timeout=5000)
            cookies = {item["name"]: item for item in context.cookies()}
            session_cookie = cookies["__Host-poolrotater-booking"]
            self.assertTrue(session_cookie["secure"])
            self.assertTrue(session_cookie["httpOnly"])
            self.assertEqual(session_cookie["sameSite"], "Lax")

            me = page.evaluate("fetch('/private-lobbies/api/me').then(r => r.json())")
            foreign_origin = page.request.post(
                "https://127.0.0.1:8443/private-lobbies/api/reserve", data="{}",
                headers={"Origin": "https://foreign-origin.invalid:8443", "Content-Type": "application/json",
                         "Cookie": session_cookie["name"] + "=" + session_cookie["value"],
                         "X-CSRF-Token": me["csrf_token"]},
            )
            self.assertEqual(foreign_origin.status, 403, "foreign Origin must be rejected by the actual portal route")
            foreign_posts = [item for item in self.server.requests
                             if item["method"] == "POST" and item["path"] == "/private-lobbies/api/reserve"
                             and item["origin"] == "https://foreign-origin.invalid:8443"]
            self.assertTrue(foreign_posts, "foreign-Origin attempt reached the actual portal route")
            foreign_post = foreign_posts[-1]
            self.assertEqual(foreign_post["status"], 403, "foreign Origin must be rejected by the actual portal route")
            self.assertNotIn("access-control-allow-origin", {key.lower() for key in foreign_post["headers"]},
                             "portal responses must not enable cross-origin reads")

            response = page.evaluate("fetch('/private-lobbies/api/reserve', {method:'POST', headers:{'Content-Type':'application/json'}, body:'{}'}).then((r) => r.status)")
            self.assertEqual(response, 403, "same-origin mutation without CSRF token must fail")

            page.get_by_role("button", name="Set up").focus()
            page.keyboard.press("Enter")
            expect(page.get_by_role("heading", name="Set up your lobby")).to_be_focused()
            friend = page.locator("#friend-id")
            friend.fill("0")
            self.assertFalse(friend.evaluate("node => node.checkValidity()"), "native min validation must reject player ID 0")
            page.get_by_role("button", name="Reserve room").click()
            self.assertIsNone(self.repository.active_for_user(OWNER))
            friend.fill(str(FRIEND))
            page.get_by_role("button", name="Reserve room").click()
            expect(page.locator("#session-state")).to_have_text(
                "Room is ready. The owner or designated friend must arrive before the timer ends.", timeout=5000)
            self.assertEqual(page.evaluate("document.activeElement.id"), "session-title")
            expect(page.locator("#claim-countdown")).not_to_have_text("Arrival window starts when the room is ready")
            expect(page.locator("#settings-block")).to_be_visible()
            unlabeled = page.locator("#session-section input, #session-section select").evaluate_all(
                "nodes => nodes.filter(node => !node.labels?.length && !node.closest('label') && !node.getAttribute('aria-label') && !node.getAttribute('aria-labelledby')).map(node => node.outerHTML)")
            self.assertEqual(unlabeled, [], "interactive settings controls must have programmatic labels")
            public = page.evaluate("fetch('/private-lobbies/api/slots').then((r) => r.text())")
            for private_value in ("password", "room_id", "players", "join_url", "selection_filters"):
                self.assertNotIn(private_value, public)

            # Browser polling must retain a local draft and block Apply after another action changes revision.
            local_min = page.locator('#settings-controls [data-setting-key="sr_min"] input[type=number]')
            local_min.fill("2.4")
            expect(page.get_by_text("Draft has unapplied changes")).to_be_visible()
            booking = self.repository.active_for_user(OWNER)
            self.assertIsNotNone(booking)
            current = self.repository.view(booking["id"], OWNER)
            self.repository.submit(booking["id"], OWNER, str(uuid.uuid4()), "start", {}, current["revision"])
            expect(page.get_by_text("The server changed. Your draft is retained; review the values before applying.")).to_be_visible(timeout=7000)
            self.assertEqual(local_min.input_value(), "2.4")
            self.assertTrue(page.get_by_role("button", name="Apply settings").is_disabled())
            expect(page.get_by_role("button", name="Reload current settings")).to_be_visible()
            page.get_by_role("button", name="Reload current settings").click()
            expect(page.get_by_text("Draft matches server settings")).to_be_visible()
            local_min = page.locator('#settings-controls [data-setting-key="sr_min"] input[type=number]')
            local_min.fill("2.4")
            page.get_by_role("button", name="Apply settings").click()
            expect(page.locator("#operation-list .operation-state").first).to_have_text("Queued", timeout=5000)
            self.assertTrue(page.get_by_role("button", name="Apply settings").is_disabled())

            # A local host observation makes the designated friend visible for an owner grant.
            configure, = [item for item in self.repository.pending(HOST)
                          if item["booking_id"] == booking["id"] and item["action"] == "configure"]
            page.evaluate("window.__privateBookingPortal.poll()")
            expect(page.locator("#operation-list .operation-state").first).to_have_text("Applying", timeout=5000)
            activity_deadline = self.clock() + 180
            self.server.set_roster(
                booking, [{"user_id": FRIEND, "username": "Invented Guest"}],
                receipt={"operation_id": configure["operation_id"], "phase": "applied", "application": "next_map"},
                effective_settings=configure["payload"],
                activity={"condition": "no_gameplay", "closes_at": activity_deadline},
            )
            expect(page.get_by_text("Invented Guest")).to_be_visible(timeout=7000)
            expect(page.locator("#session-state")).to_have_text("The private lobby is active.")
            expect(page.locator("#current-map")).to_contain_text("Invented current map")
            expect(page.locator("#next-maps")).to_contain_text("Invented queued map")
            expect(page.locator("#settings-state")).to_contain_text("Settings applied for the next map selection.")
            expect(page.locator("#settings-effects")).to_contain_text(
                "The current map and maps already in the visible queue stay unchanged.")
            expect(page.locator("#operation-list .operation-state").first).to_have_text("Applied for next map")
            expect(page.locator("#activity-status")).to_contain_text("30 minutes without meaningful gameplay")
            expect(page.locator("#activity-status")).to_contain_text("Current host close estimate:")
            expect(page.locator("#activity-status")).to_contain_text(re.compile(r"\d+:\d{2}"))
            self.assertIn("notice-warning", page.locator("#activity-status").get_attribute("class"))

            # A sleeping/offline page must age its cached report locally instead of
            # keeping the prior deadline visible until a successful poll arrives.
            page.evaluate("() => { window.__bookingNowOffset += 31000; window.__privateBookingPortal.renderActivity(window.__privateBookingPortal.booking, true); }")
            expect(page.locator("#activity-status")).to_contain_text("Activity timing is delayed or unavailable.")
            self.assertNotIn("Current host close estimate:", page.locator("#activity-status").inner_text())
            page.evaluate("() => { window.__bookingNowOffset -= 31000; window.__privateBookingPortal.renderActivity(window.__privateBookingPortal.booking, true); }")
            expect(page.locator("#activity-status")).to_contain_text("Current host close estimate:")

            short_deadline = self.clock() + 15
            self.server.set_roster(booking, [{"user_id": FRIEND, "username": "Invented Guest"}],
                                   activity={"condition": "no_gameplay", "closes_at": short_deadline})
            page.evaluate("window.__privateBookingPortal.poll()")
            page.evaluate("() => { window.__bookingNowOffset += 16000; window.__privateBookingPortal.renderActivity(window.__privateBookingPortal.booking, true); }")
            expect(page.locator("#activity-status")).to_contain_text(
                "The reported timer has elapsed; waiting for a fresh host report to confirm status.")
            expect(page.locator("#session-state")).to_have_text("The private lobby is active.")
            self.assertNotIn("ended", page.locator("#session-state").inner_text().lower())
            page.evaluate("() => { window.__bookingNowOffset -= 16000; window.__privateBookingPortal.renderActivity(window.__privateBookingPortal.booking, true); }")
            self.server.set_roster(booking, [{"user_id": FRIEND, "username": "Invented Guest"}],
                                   activity={"condition": "no_gameplay", "closes_at": activity_deadline})
            page.evaluate("window.__privateBookingPortal.poll()")

            # Age the last host observation past the existing freshness bound. A page poll
            # must hide the deadline; a later host report retains its original absolute time.
            self.clock.advance(31)
            page.evaluate("() => { window.__bookingNowOffset += 31000; window.__privateBookingPortal.renderActivity(window.__privateBookingPortal.booking, true); }")
            expect(page.locator("#activity-status")).to_contain_text(
                "Activity timing is delayed or unavailable.", timeout=7000)
            self.assertNotIn("Current host close estimate:", page.locator("#activity-status").inner_text())
            page.evaluate("window.__privateBookingPortal.poll()")
            expect(page.locator("#activity-status")).to_contain_text(
                "Activity timing is delayed or unavailable.", timeout=7000)
            self.server.set_roster(booking, [{"user_id": FRIEND, "username": "Invented Guest"}],
                                   activity={"condition": "no_gameplay", "closes_at": activity_deadline})
            page.evaluate("window.__privateBookingPortal.poll()")
            expect(page.locator("#activity-status")).to_contain_text("Current host close estimate:", timeout=7000)
            current_activity = self.repository.view(booking["id"], OWNER)["activity"]
            self.assertEqual(current_activity["closes_at"], activity_deadline,
                             "browser polling must not extend the host's absolute deadline")
            self.assertLess(current_activity["closes_at"], self.clock() + 1800)
            self.server.set_roster(booking, [],
                                   activity={"condition": "empty", "closes_at": activity_deadline})
            page.evaluate("window.__privateBookingPortal.poll()")
            expect(page.locator("#activity-status")).to_contain_text(
                "30 continuous minutes empty, or earlier if 30 minutes pass without meaningful gameplay",
                timeout=7000)
            self.assertEqual(self.repository.view(booking["id"], OWNER)["activity"]["closes_at"],
                             activity_deadline)
            self.server.set_roster(booking, [{"user_id": FRIEND, "username": "Invented Guest"}],
                                   activity={"condition": "no_gameplay", "closes_at": activity_deadline})
            page.evaluate("window.__privateBookingPortal.poll()")
            map_deadline = self.clock() + 4120
            self.server.set_roster(booking, [{"user_id": FRIEND, "username": "Invented Guest"}],
                                   activity={"condition": "map_in_progress", "closes_at": map_deadline})
            page.evaluate("window.__privateBookingPortal.poll()")
            expect(page.locator("#activity-status")).to_contain_text(
                "A verified map is in progress. The host uses its expected end plus a short grace")
            expect(page.locator("#activity-status")).to_contain_text("Current host close estimate:")
            self.assertNotIn("notice-warning", page.locator("#activity-status").get_attribute("class"))
            self.server.set_roster(booking, [{"user_id": FRIEND, "username": "Invented Guest"}],
                                   activity={"condition": "no_gameplay", "closes_at": activity_deadline})
            page.evaluate("window.__privateBookingPortal.poll()")
            roster_revision = self.repository.view(booking["id"], OWNER)["revision"]
            page.wait_for_function("revision => window.__privateBookingPortal?.booking?.revision === revision",
                                   arg=roster_revision, timeout=7000)
            operation_count = len(self.repository.view(booking["id"], OWNER)["operations"])
            page.get_by_role("button", name="Make controller").click()
            expect(page.locator("#operation-list .operation-state").first).to_have_text("pending", timeout=5000)
            self.assertEqual(len(self.repository.view(booking["id"], OWNER)["operations"]), operation_count + 1)
            grant = self.repository.view(booking["id"], OWNER)["operations"][-1]
            self.server.set_roster(booking, [{"user_id": FRIEND, "username": "Invented Guest"}],
                                   receipt={"operation_id": grant["id"], "phase": "applied"})
            expect(page.get_by_role("button", name="Remove controller")).to_be_visible(timeout=7000)
            after_grant = self.repository.view(booking["id"], OWNER)["revision"]
            page.wait_for_function("revision => window.__privateBookingPortal?.booking?.revision === revision",
                                   arg=after_grant, timeout=7000)

            # A separate simulated account can read the booking only while its session role is active.
            self.portal.oauth.next_actor = "friend"
            guest_context = browser.new_context(viewport={"width": 768, "height": 900}, ignore_https_errors=True)
            guest_page = guest_context.new_page()
            guest_page.goto(SITE + "/private-lobbies/", wait_until="networkidle")
            guest_page.keyboard.press("Tab")
            guest_page.keyboard.press("Enter")
            expect(guest_page.get_by_role("button", name="Sign out")).to_be_visible(timeout=5000)
            guest_access = guest_page.evaluate("""async (bookingId) => {
              const me = await fetch('/private-lobbies/api/me').then(r => r.json());
              const response = await fetch('/private-lobbies/api/session?id=' + encodeURIComponent(bookingId));
              const session = await response.json();
              async function action(name, target) {
                const result = await fetch('/private-lobbies/api/action', {method:'POST', headers:{
                  'Content-Type':'application/json', 'X-CSRF-Token':me.csrf_token
                }, body:JSON.stringify({request_id:crypto.randomUUID(), booking_id:bookingId,
                  expected_revision:session.booking.revision, action:name, payload:{user_id:target}})});
                return result.status;
              }
              return {status:response.status, role:session.booking?.role,
                grant:await action('grant', 909), revoke:await action('revoke', 808)};
            }""", booking["id"])
            self.assertEqual(guest_access, {"status": 200, "role": "controller", "grant": 403, "revoke": 403})

            page.get_by_role("button", name="Remove controller").click()
            expect(page.locator("#operation-list .operation-state").first).to_have_text("pending", timeout=5000)
            revoke = self.repository.view(booking["id"], OWNER)["operations"][-1]
            self.server.set_roster(booking, [{"user_id": FRIEND, "username": "Invented Guest"}],
                                   receipt={"operation_id": revoke["id"], "phase": "applied"})
            expect(page.locator("#operation-list .operation-state").first).to_have_text("Applied", timeout=7000)
            expect(page.get_by_role("button", name="Make controller")).to_be_visible(timeout=7000)
            self.assertEqual(guest_page.evaluate("""async (bookingId) =>
              (await fetch('/private-lobbies/api/session?id=' + encodeURIComponent(bookingId))).status""",
              booking["id"]), 403)

            # Countdown settings are reported as live; selection settings are reported for the next map.
            self.clock.advance(11)
            page.evaluate("""() => {
              window.__bookingNowOffset += 11000;
              window.__privateBookingPortal.renderSettingsStatus();
            }""")
            countdown = page.locator('#settings-controls [data-setting-key="countdown_seconds"] input[type=number]')
            countdown.fill("25")
            page.get_by_role("button", name="Apply settings").click()
            expect(page.locator("#operation-list .operation-state").first).to_have_text("Queued", timeout=5000)
            live_configure, = [item for item in self.repository.pending(HOST)
                               if item["booking_id"] == booking["id"] and item["action"] == "configure"]
            self.server.set_roster(
                booking, [{"user_id": FRIEND, "username": "Invented Guest"}],
                receipt={"operation_id": live_configure["operation_id"], "phase": "applied", "application": "live"},
                effective_settings=live_configure["payload"],
            )
            expect(page.locator("#settings-state")).to_contain_text("Settings applied live in the room.", timeout=7000)
            expect(page.locator("#settings-effects")).to_contain_text(
                "Room controls covered by this update use the new settings now.")
            expect(page.locator("#operation-list .operation-state").first).to_have_text("Applied live")
            live_revision = self.repository.view(booking["id"], OWNER)["revision"]
            page.wait_for_function("revision => window.__privateBookingPortal?.booking?.revision === revision",
                                   arg=live_revision, timeout=7000)

            self.clock.advance(11)
            page.evaluate("""() => {
              window.__bookingNowOffset += 11000;
              window.__privateBookingPortal.renderSettingsStatus();
            }""")
            countdown.fill("30")
            page.get_by_role("button", name="Apply settings").click()
            expect(page.locator("#operation-list .operation-state").first).to_have_text("Queued", timeout=5000)
            failed_configure, = [item for item in self.repository.pending(HOST)
                                 if item["booking_id"] == booking["id"] and item["action"] == "configure"]
            page.evaluate("window.__privateBookingPortal.poll()")
            expect(page.locator("#operation-list .operation-state").first).to_have_text("Applying", timeout=5000)
            self.server.set_roster(
                booking, [{"user_id": FRIEND, "username": "Invented Guest"}],
                receipt={"operation_id": failed_configure["operation_id"],
                         "phase": "failed", "error": "worker_action_outcome_unknown"},
            )
            expect(page.locator("#settings-state")).to_contain_text(
                "Settings were not confirmed.", timeout=7000)
            expect(page.locator("#settings-effects")).to_contain_text(
                "The room did not confirm this update.")
            expect(countdown).to_have_value("25")
            expect(page.locator("#operation-list .operation-state").first).to_have_text("Not applied")

            page.get_by_role("button", name="Create invite link").click()
            expect(page.get_by_role("link", name="Open invite")).to_be_visible(timeout=5000)
            expect(page.get_by_role("button", name="Copy invite link")).to_be_visible()
            context.grant_permissions(["clipboard-read", "clipboard-write"], origin=SITE)
            page.get_by_role("button", name="Copy invite link").click()
            expect(page.locator("#page-message")).to_contain_text("Invite link copied.")
            copied = page.evaluate("navigator.clipboard.readText()")
            self.assertIn("/private-lobbies/join?invite=", copied)
            page.evaluate("""() => Object.defineProperty(navigator, 'clipboard', {configurable:true,
              value:{writeText:() => Promise.reject(new DOMException('denied', 'NotAllowedError'))}})""")
            page.get_by_role("button", name="Copy invite link").click()
            expect(page.locator("#page-message")).to_contain_text("The invite link could not be copied.")
            invite = context.new_page()
            invite.goto(copied, wait_until="domcontentloaded")
            expect(invite.get_by_role("heading", name="Join private lobby")).to_be_visible()
            expect(invite.get_by_role("link", name="Join private lobby")).to_have_attribute(
                "href", "https://osu.ppy.sh/multiplayer/rooms/998877")
            invite.close()

            # Exercise the real responsive layout after authenticated controls render.
            for width in (360, 768, 1280):
                page.set_viewport_size({"width": width, "height": 900})
                self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"), width)

            page.once("dialog", lambda dialog: dialog.accept())
            page.get_by_role("button", name="End booking").click()
            expect(page.locator("#session-state")).to_have_text(
                "The room is closing and cleaning up.", timeout=7000)
            guest_context.close()
            page.get_by_role("button", name="Sign out").click()
            expect(page.get_by_role("link", name="Sign in")).to_be_visible(timeout=5000)
            post_logout = page.evaluate("fetch('/private-lobbies/api/slots').then(r => r.text())")
            for private_value in ("password", "room_id", "players", "join_url", "selection_filters"):
                self.assertNotIn(private_value, post_logout)
            self.assertNotIn('"activity_updated_at"', post_logout)
            self.assertEqual(errors, [], f"browser console exceptions: {errors}")
            context.close()
            browser.close()


if __name__ == "__main__":
    unittest.main()
