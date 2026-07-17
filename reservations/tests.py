import json
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

from django.db.models.signals import post_save
from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone

from django.contrib.auth.models import User

from rates.models import Location, Route, Vehicle, Rate
from reservations.models import Customer, Leg, Lead, Reservation
from users.models import TravelAgent
from users.signals import travel_agent_email
from reservations.lead_matching import (
    ReservationIndex, match_lead, norm_phone, recheck_lead_conversions,
)
from reservations.signals import (
    auto_convert_lead_on_reservation, reservation_saved,
    sync_lead_status_to_ghl, sync_lead_to_ghl_on_create,
)


@override_settings(GHL_API_KEY="", GHL_LOCATION_ID="")
class ConvergeDuplicateLeadsTests(TestCase):
    """
    Booking converts the matched lead AND its same-trip duplicate twins, so a
    booked customer never keeps a stale "interested" lead (the source of the
    pre-pickup nudge being sent to people who already booked).
    """

    @classmethod
    def setUpTestData(cls):
        origin = Location.objects.create(name="MCO Airport")
        dest = Location.objects.create(name="Disney World")
        route = Route.objects.create(origin=origin, destination=dest)
        vehicle = Vehicle.objects.create(
            vehicle_type="suv", capacity=6, luggage_capacity=6
        )
        cls.rate = Rate.objects.create(
            vehicle=vehicle, route=route,
            oneway_price=Decimal("140"), round_trip_price=Decimal("275"),
        )

    def setUp(self):
        # Disconnect the unrelated background-thread signals (reservation email +
        # per-Lead GHL sync) so the in-test DB isn't touched from worker threads;
        # only the conversion signal under test runs. Reconnect after each test.
        post_save.disconnect(reservation_saved, sender=Reservation)
        post_save.disconnect(sync_lead_to_ghl_on_create, sender=Lead)
        self.addCleanup(
            lambda: post_save.connect(reservation_saved, sender=Reservation)
        )
        self.addCleanup(
            lambda: post_save.connect(sync_lead_to_ghl_on_create, sender=Lead)
        )

    def _make_reservation(self):
        customer = Customer.objects.create(
            first_name="Cherish", last_name="Dobbins",
            email="dup@example.com", phone_number="804-787-0255",
        )
        return Reservation.objects.create(
            trip_type="oneway", customer=customer, rate=self.rate,
            base_price=Decimal("275"), total_price=Decimal("275"),
            status="confirmed",
        )

    def _lead(self, **kw):
        defaults = dict(
            first_name="Cherish", last_name="Dobbins",
            email="dup@example.com", phone="804-787-0255",
            pickup_date=timezone.localdate() + timedelta(days=3),
            status=Lead.StatusChoices.INTERESTED,
        )
        defaults.update(kw)
        return Lead.objects.create(**defaults)

    def test_same_trip_twin_is_converged(self):
        # A round-trip + one-way quote (same person, same date) = two leads.
        rt = self._lead(trip_type="roundtrip", estimated_price=Decimal("275"))
        ow = self._lead(trip_type="oneway", estimated_price=Decimal("140"))
        self._make_reservation()
        rt.refresh_from_db()
        ow.refresh_from_db()
        # Both twins end up converted — neither remains nudge-eligible.
        self.assertTrue(rt.converted)
        self.assertTrue(ow.converted)
        self.assertEqual(rt.status, Lead.StatusChoices.CONVERTED)
        self.assertEqual(ow.status, Lead.StatusChoices.CONVERTED)

    def test_different_date_trip_is_left_alone(self):
        # The matched primary is the most-recently-created active lead; make the
        # day-3 lead the most recent so it is the primary, and assert the
        # genuinely separate day-20 trip is NOT swept into "converted".
        other = self._lead(
            pickup_date=timezone.localdate() + timedelta(days=20),
            created_at=timezone.now() - timedelta(hours=1),
        )
        same = self._lead()  # day-3, created now → matched as primary
        self._make_reservation()
        same.refresh_from_db()
        other.refresh_from_db()
        self.assertTrue(same.converted)
        self.assertFalse(other.converted)
        self.assertEqual(other.status, Lead.StatusChoices.INTERESTED)


def _res_row(rid, *, email="", phone="", created_at=None):
    return {
        "id": rid, "created_at": created_at,
        "customer__email": email, "customer__phone_number": phone,
    }


def _lead_like(*, email="", phone=""):
    return SimpleNamespace(email=email, normalized_phone=norm_phone(phone))


class LeadMatcherUnitTests(SimpleTestCase):
    """Pure matching logic (no DB): email first, then phone, newest booking wins."""

    def _index(self, *rows):
        return ReservationIndex.build(rows)

    def test_email_match(self):
        idx = self._index(_res_row(3, email="a@x.com", phone="800-000-0000"))
        self.assertEqual(match_lead(_lead_like(email="A@X.com", phone="800-999-9999"), idx), 3)

    def test_email_preferred_over_phone(self):
        idx = self._index(
            _res_row(3, email="a@x.com", phone="800-000-0000"),
            _res_row(4, email="other@x.com", phone="800-999-9999"),
        )
        # Lead's email matches res 3; its phone matches res 4 — email wins.
        self.assertEqual(match_lead(_lead_like(email="a@x.com", phone="800-999-9999"), idx), 3)

    def test_phone_match_when_no_email_hit(self):
        idx = self._index(_res_row(4, email="booker@x.com", phone="800-555-1212"))
        self.assertEqual(match_lead(_lead_like(email="nomatch@y.com", phone="(800) 555-1212"), idx), 4)

    def test_newest_reservation_wins(self):
        import datetime
        older = _res_row(10, email="a@x.com", created_at=datetime.datetime(2026, 1, 1))
        newer = _res_row(11, email="a@x.com", created_at=datetime.datetime(2026, 5, 1))
        self.assertEqual(match_lead(_lead_like(email="a@x.com"), self._index(older, newer)), 11)

    def test_no_match(self):
        idx = self._index(_res_row(9, email="a@x.com", phone="800-000-0000"))
        self.assertIsNone(match_lead(_lead_like(email="z@z.com", phone="111-111-1111"), idx))


@override_settings(GHL_API_KEY="", GHL_LOCATION_ID="")
class RecheckLeadConversionsEngineTests(TestCase):
    """The bulk engine: email/phone matches convert + link reservation; no-match
    leads are left alone; dry-run writes nothing."""

    @classmethod
    def setUpTestData(cls):
        origin = Location.objects.create(name="MCO Airport")
        dest = Location.objects.create(name="Disney World")
        route = Route.objects.create(origin=origin, destination=dest)
        vehicle = Vehicle.objects.create(vehicle_type="suv", capacity=6, luggage_capacity=6)
        cls.rate = Rate.objects.create(
            vehicle=vehicle, route=route,
            oneway_price=Decimal("140"), round_trip_price=Decimal("275"),
        )

    def setUp(self):
        # Isolate the ENGINE: stop the real-time conversion signal from converting
        # leads at Reservation-create time, and silence background-thread signals.
        for sig, sender in [
            (reservation_saved, Reservation),
            (auto_convert_lead_on_reservation, Reservation),
            (sync_lead_to_ghl_on_create, Lead),
            (sync_lead_status_to_ghl, Lead),
        ]:
            post_save.disconnect(sig, sender=sender)
            self.addCleanup(lambda s=sig, snd=sender: post_save.connect(s, sender=snd))

    def _reservation(self, *, email, phone, last_name="Booker"):
        customer = Customer.objects.create(
            first_name="Test", last_name=last_name, email=email, phone_number=phone,
        )
        return Reservation.objects.create(
            trip_type="oneway", customer=customer, rate=self.rate,
            base_price=Decimal("140"), total_price=Decimal("140"),
            status="confirmed",
        )

    def _lead(self, **kw):
        defaults = dict(
            first_name="Lead", last_name="Person", email="lead@x.com",
            phone="800-555-0000", status=Lead.StatusChoices.INTERESTED,
        )
        defaults.update(kw)
        return Lead.objects.create(**defaults)

    def test_email_match_converts_and_links(self):
        res = self._reservation(email="cust@x.com", phone="111-222-3333")
        lead = self._lead(email="cust@x.com", phone="999-888-7777")
        report = recheck_lead_conversions(Lead.objects.all())
        lead.refresh_from_db()
        self.assertEqual(report.converted, 1)
        self.assertTrue(lead.converted)
        self.assertEqual(lead.converted_reservation_id, res.id)

    def test_phone_match_converts_and_links(self):
        res = self._reservation(email="booker@x.com", phone="800-555-1212")
        lead = self._lead(email="nomatch@y.com", phone="(800) 555-1212")
        report = recheck_lead_conversions(Lead.objects.all())
        lead.refresh_from_db()
        self.assertEqual(report.converted, 1)
        self.assertTrue(lead.converted)
        self.assertEqual(lead.converted_reservation_id, res.id)

    def test_no_reservation_means_no_conversion(self):
        lead = self._lead(email="lonely@x.com", phone="000-000-0000")
        report = recheck_lead_conversions(Lead.objects.all())
        lead.refresh_from_db()
        self.assertEqual(report.no_match, 1)
        self.assertFalse(lead.converted)

    def test_dry_run_writes_nothing(self):
        self._reservation(email="cust@x.com", phone="111-222-3333")
        lead = self._lead(email="cust@x.com")
        report = recheck_lead_conversions(Lead.objects.all(), dry_run=True)
        lead.refresh_from_db()
        self.assertEqual(report.converted, 1)
        self.assertFalse(lead.converted)  # nothing persisted

    def test_already_converted_is_skipped(self):
        self._reservation(email="cust@x.com", phone="111-222-3333")
        self._lead(email="cust@x.com", status=Lead.StatusChoices.CONVERTED, converted=True)
        report = recheck_lead_conversions(Lead.objects.all())
        self.assertEqual(report.already_converted, 1)
        self.assertEqual(report.converted, 0)


@override_settings(GHL_API_KEY="", GHL_LOCATION_ID="")
class AgentAutoAttachByEmailTests(TestCase):
    """A reservation whose customer (booking-contact) email matches a registered
    ACTIVE travel agent auto-links to that agent at creation -- agents book for
    their clients under their own email, so the trip lands in the agent's portal
    with no manual step, and the commission auto-calculates from the linked agent.
    Creation-only + only-when-unset, so explicit picks and manual detaches win.
    """

    @classmethod
    def setUpTestData(cls):
        origin = Location.objects.create(name="MCO Airport")
        dest = Location.objects.create(name="Disney World")
        route = Route.objects.create(origin=origin, destination=dest)
        vehicle = Vehicle.objects.create(vehicle_type="suv", capacity=6, luggage_capacity=6)
        cls.rate = Rate.objects.create(
            vehicle=vehicle, route=route,
            oneway_price=Decimal("140"), round_trip_price=Decimal("275"),
        )
        active_user = User.objects.create_user(username="agentjane", email="jane@agency.com")
        cls.agent = TravelAgent.objects.create(
            user=active_user, agent_name="Jane Doe", phone="555-0100",
            commission_rate=Decimal("15.00"), is_active=True,
        )
        inactive_user = User.objects.create_user(username="agentold", email="old@agency.com")
        cls.inactive_agent = TravelAgent.objects.create(
            user=inactive_user, agent_name="Old Agent", phone="555-0199", is_active=False,
        )

    def setUp(self):
        # Silence background-thread + welcome-email signals so the test DB isn't
        # touched from worker threads; the feature under test is in Reservation.save().
        for sig, sender in [
            (reservation_saved, Reservation),
            (auto_convert_lead_on_reservation, Reservation),
            (sync_lead_to_ghl_on_create, Lead),
            (sync_lead_status_to_ghl, Lead),
            (travel_agent_email, TravelAgent),
        ]:
            post_save.disconnect(sig, sender=sender)
            self.addCleanup(lambda s=sig, snd=sender: post_save.connect(s, sender=snd))

    def _reservation(self, email, *, travel_agent=None):
        customer = Customer.objects.create(
            first_name="Client", last_name="Smith", email=email, phone_number="111-222-3333",
        )
        return Reservation.objects.create(
            trip_type="oneway", customer=customer, rate=self.rate,
            base_price=Decimal("200"), total_price=Decimal("200"),
            status="confirmed", travel_agent=travel_agent,
        )

    def test_matching_email_attaches_agent_and_calcs_commission(self):
        res = self._reservation("jane@agency.com")
        self.assertEqual(res.travel_agent_id, self.agent.id)
        # 15% of base_price 200 = 30.00, calculated because the agent was linked
        # before the commission block in Reservation.save().
        self.assertEqual(res.commission_amount, Decimal("30.00"))

    def test_match_is_case_insensitive(self):
        res = self._reservation("JANE@Agency.com")
        self.assertEqual(res.travel_agent_id, self.agent.id)

    def test_no_matching_agent_leaves_unattached(self):
        res = self._reservation("walkin-customer@gmail.com")
        self.assertIsNone(res.travel_agent_id)

    def test_inactive_agent_is_not_attached(self):
        res = self._reservation("old@agency.com")
        self.assertIsNone(res.travel_agent_id)

    def test_explicit_agent_on_create_is_not_overridden(self):
        other_user = User.objects.create_user(username="agentbob", email="bob@agency.com")
        other = TravelAgent.objects.create(
            user=other_user, phone="555-0123", is_active=True,
            commission_rate=Decimal("10.00"),  # Decimal, as a DB-loaded agent always is
        )
        # Booked with a deliberately chosen agent; the email-derived one must NOT win.
        res = self._reservation("jane@agency.com", travel_agent=other)
        self.assertEqual(res.travel_agent_id, other.id)

    def test_manual_detach_sticks_on_edit(self):
        res = self._reservation("jane@agency.com")
        self.assertEqual(res.travel_agent_id, self.agent.id)
        res.travel_agent = None
        res.save()
        res.refresh_from_db()
        # Hook is creation-only, so a later deliberate detach is never re-applied.
        self.assertIsNone(res.travel_agent_id)

    def test_booking_source_attributes_to_agent_even_for_staff(self):
        from reservations.attribution import derive_booking_source
        res = self._reservation("jane@agency.com")
        staff_request = SimpleNamespace(
            user=SimpleNamespace(is_authenticated=True, is_staff=True)
        )
        # Agent attribution wins over the staff/phone label (canonical "agent wins").
        self.assertEqual(derive_booking_source(res, request=staff_request), "travel_agent")


@override_settings(GHL_API_KEY="", GHL_LOCATION_ID="")
class VipFlagTests(TestCase):
    """`Leg.is_vip` is the single source of truth for the board's gold VIP
    highlight: True when the reservation is manually flagged VIP OR its travel
    agent belongs to a VIP agency (Small World Big Fun). The toggle endpoint
    flips the per-reservation manual flag from the dispatch dashboard."""

    @classmethod
    def setUpTestData(cls):
        origin = Location.objects.create(name="MCO Airport")
        dest = Location.objects.create(name="Disney World")
        route = Route.objects.create(origin=origin, destination=dest)
        vehicle = Vehicle.objects.create(vehicle_type="suv", capacity=6, luggage_capacity=6)
        cls.rate = Rate.objects.create(
            vehicle=vehicle, route=route,
            oneway_price=Decimal("140"), round_trip_price=Decimal("275"),
        )

    def setUp(self):
        post_save.disconnect(reservation_saved, sender=Reservation)
        post_save.disconnect(travel_agent_email, sender=TravelAgent)
        self.addCleanup(lambda: post_save.connect(reservation_saved, sender=Reservation))
        self.addCleanup(lambda: post_save.connect(travel_agent_email, sender=TravelAgent))

    def _reservation(self, **kw):
        customer = Customer.objects.create(
            first_name="V", last_name="IP", email="walkin@x.com", phone_number="111",
        )
        defaults = dict(
            trip_type="oneway", customer=customer, rate=self.rate,
            base_price=Decimal("140"), total_price=Decimal("140"), status="confirmed",
        )
        defaults.update(kw)
        return Reservation.objects.create(**defaults)

    def _leg(self, res):
        return Leg.objects.create(
            reservation=res, route=self.rate.route, vehicle=self.rate.vehicle,
            pickup_date=timezone.localdate(), pickup_time=timezone.now().time(),
        )

    def test_manual_flag_makes_leg_vip(self):
        leg = self._leg(self._reservation(is_vip=True))
        self.assertTrue(leg.is_vip)

    def test_plain_reservation_leg_is_not_vip(self):
        leg = self._leg(self._reservation())
        self.assertFalse(leg.is_vip)

    def test_swbf_agency_agent_makes_leg_vip(self):
        u = User.objects.create_user(username="swbf", email="swbf@x.com")
        agent = TravelAgent.objects.create(
            user=u, phone="555", agency_name="Small World Big Fun Travel",
            commission_rate=Decimal("10.00"),  # Decimal, as a DB-loaded agent always is
        )
        leg = self._leg(self._reservation(travel_agent=agent))
        self.assertTrue(leg.is_vip)  # VIP via agency keyword, no manual flag needed

    def test_other_agency_agent_leg_is_not_vip(self):
        u = User.objects.create_user(username="other", email="o@x.com")
        agent = TravelAgent.objects.create(
            user=u, phone="555", agency_name="Regular Travel Co",
            commission_rate=Decimal("10.00"),  # Decimal, as a DB-loaded agent always is
        )
        leg = self._leg(self._reservation(travel_agent=agent))
        self.assertFalse(leg.is_vip)

    def test_toggle_endpoint_sets_and_clears(self):
        from django.urls import reverse
        staff = User.objects.create_user(username="disp", password="x", is_staff=True)
        self.client.force_login(staff)
        res = self._reservation()
        url = reverse("toggle_reservation_vip")

        resp = self.client.post(
            url, data=json.dumps({"reservation_id": res.id, "is_vip": True}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["is_vip"])
        res.refresh_from_db()
        self.assertTrue(res.is_vip)

        resp = self.client.post(
            url, data=json.dumps({"reservation_id": res.id, "is_vip": False}),
            content_type="application/json",
        )
        self.assertFalse(resp.json()["is_vip"])
        res.refresh_from_db()
        self.assertFalse(res.is_vip)

    def test_toggle_endpoint_requires_staff(self):
        from django.urls import reverse
        res = self._reservation()
        # Not logged in -> staff_member_required redirects to admin login, no change.
        resp = self.client.post(
            reverse("toggle_reservation_vip"),
            data=json.dumps({"reservation_id": res.id, "is_vip": True}),
            content_type="application/json",
        )
        self.assertIn(resp.status_code, (301, 302))
        res.refresh_from_db()
        self.assertFalse(res.is_vip)

    def test_planner_schedule_slot_carries_vip(self):
        # The planner timeline is built by build_driver_schedules; its slots must
        # carry is_vip so the gold ring renders. Exercises the real data path.
        from dispatching.scheduler import build_driver_schedules, preload_timing_cache
        from drivers.models import Driver
        preload_timing_cache()  # empty in test DB -> drive-time falls back, no per-call DB
        u = User.objects.create_user(username="drv", first_name="Dee", last_name="River")
        driver = Driver.objects.create(profile=u, driver_type="inhouse")
        res = self._reservation(is_vip=True)
        leg = Leg.objects.create(
            reservation=res, route=self.rate.route, vehicle=self.rate.vehicle,
            pickup_date=timezone.localdate(), pickup_time=timezone.now().time(),
            driver=driver, status="confirmed",
        )
        scheds = build_driver_schedules([leg], [driver], timezone.localdate())
        slots = scheds[driver.id].slots
        self.assertTrue(slots)
        self.assertTrue(slots[0].is_vip)

    def test_legs_board_shows_display_badge_for_vip_only(self):
        # Legs are display-only now: a VIP reservation's leg shows the badge,
        # a non-VIP one shows nothing and there is NO per-leg toggle to misclick.
        from django.urls import reverse
        staff = User.objects.create_user(username="disp3", password="x", is_staff=True)
        self.client.force_login(staff)
        today = timezone.localdate()
        url = reverse("legs_list")
        params = {"date_from": today.isoformat(), "date_to": today.isoformat()}
        res = self._reservation(is_vip=True)
        self._leg(res)

        resp = self.client.get(url, params)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'title="VIP reservation"')  # display badge rendered
        self.assertNotContains(resp, "vip-toggle")            # no per-leg toggle control

        res.is_vip = False
        res.save(update_fields=["is_vip"])
        resp2 = self.client.get(url, params)
        self.assertNotContains(resp2, 'title="VIP reservation"')  # badge gone when not VIP

    def test_reservation_page_has_vip_toggle(self):
        # VIP is set at the reservation level: the detail page carries the toggle.
        from django.urls import reverse
        staff = User.objects.create_user(username="disp5", password="x", is_staff=True)
        self.client.force_login(staff)
        res = self._reservation()
        self._leg(res)
        resp = self.client.get(reverse("reservation_details", args=[res.uuid]))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'id="vipToggleBtn"')


class ChannelClassificationTests(SimpleTestCase):
    """`classify_channel` / `derive_booking_source` map raw UTM / click-ID /
    referrer signals onto the granular acquisition-channel taxonomy that the
    revenue + Reservation Sources dashboards group by. The point of these is to
    pin the NEW channels (Bing, ChatGPT, Gemini, ...) so they never silently
    collapse back into 'direct' the way they did before."""

    def _classify(self, **kw):
        from reservations.attribution import classify_channel
        return classify_channel(**kw)

    def test_ai_assistants_each_get_their_own_channel(self):
        # ChatGPT appends ?utm_source=chatgpt.com to cited links.
        self.assertEqual(self._classify(src="chatgpt.com"), "chatgpt")
        self.assertEqual(self._classify(src="ChatGPT"), "chatgpt")
        # Other assistants resolve via the referrer host (no UTM tag).
        self.assertEqual(self._classify(referrer_host="gemini.google.com"), "gemini")
        self.assertEqual(self._classify(referrer_host="www.perplexity.ai"), "perplexity")
        self.assertEqual(self._classify(referrer_host="copilot.microsoft.com"), "copilot")

    def test_bing_splits_ads_vs_organic_like_google(self):
        self.assertEqual(self._classify(src="bing", medium="cpc"), "bing_ads")
        self.assertEqual(self._classify(src="bing", medium=None), "bing_organic")
        self.assertEqual(self._classify(referrer_host="bing.com"), "bing_organic")

    def test_google_and_meta_behaviour_is_unchanged(self):
        self.assertEqual(self._classify(gclid="abc"), "google_ads")
        self.assertEqual(self._classify(src="google", medium="cpc"), "google_ads")
        self.assertEqual(self._classify(src="google", medium="organic"), "google_organic")
        self.assertEqual(self._classify(fbclid="z", medium="cpc"), "meta_ads")
        self.assertEqual(self._classify(src="facebook"), "meta_organic")

    def test_referrer_is_only_a_fallback_when_no_utm(self):
        # An explicit Bing-ads UTM wins over a (stale) chatgpt referrer cookie.
        self.assertEqual(
            self._classify(src="bing", medium="cpc", referrer_host="chatgpt.com"),
            "bing_ads",
        )

    def test_unknown_tagged_source_is_preserved_not_hidden(self):
        # A brand-new tagged source surfaces as its own channel (slugified),
        # never folded into 'direct' — that's the whole "track every channel" ask.
        self.assertEqual(self._classify(src="Some Partner"), "some_partner")
        # An unrecognized EXTERNAL referrer is a generic referral, not direct.
        self.assertEqual(self._classify(referrer_host="randomblog.example"), "referral")

    def test_no_signal_is_direct(self):
        self.assertEqual(self._classify(), "direct")
        self.assertEqual(self._classify(src="", medium="", referrer_host=""), "direct")

    def test_derive_precedence_agent_then_staff_then_channel(self):
        from reservations.attribution import derive_booking_source
        # Agent FK beats everything (even a Bing click ID).
        agent_res = SimpleNamespace(travel_agent_id=7, utm_source="bing", gclid="x")
        self.assertEqual(derive_booking_source(agent_res), "travel_agent")
        # Staff request (no agent) -> phone.
        staff_req = SimpleNamespace(user=SimpleNamespace(is_authenticated=True, is_staff=True))
        staff_res = SimpleNamespace(
            travel_agent_id=None, utm_source="chatgpt.com",
            gclid=None, fbclid=None, utm_medium=None, referrer_host=None,
        )
        self.assertEqual(derive_booking_source(staff_res, request=staff_req), "phone")
        # Public visitor (no agent, no staff) -> the real channel.
        pub_res = SimpleNamespace(
            travel_agent_id=None, utm_source="chatgpt.com",
            gclid=None, fbclid=None, utm_medium=None, referrer_host=None,
        )
        self.assertEqual(derive_booking_source(pub_res), "chatgpt")

    def test_referrer_host_field_drives_organic_ai_attribution(self):
        # Visitor arrives from ChatGPT with NO utm tag — only the referrer host.
        from reservations.attribution import derive_booking_source
        res = SimpleNamespace(
            travel_agent_id=None, utm_source=None, utm_medium=None,
            gclid=None, fbclid=None, referrer_host="chatgpt.com",
        )
        self.assertEqual(derive_booking_source(res), "chatgpt")

    def test_every_taxonomy_channel_has_a_label(self):
        from reservations.attribution import CHANNEL_LABELS, CHANNEL_GROUPS
        for _key, _label, _color, subs in CHANNEL_GROUPS:
            for slug in subs:
                self.assertIn(slug, CHANNEL_LABELS, f"{slug} missing a label")


class ReservationPaymentStatusTests(TestCase):
    """
    payment_status must aggregate across ALL Payment rows with precedence
    paid > card_saved > pending, not return on the first row in DB order.
    Regression: a booking-time card_saved (or abandoned pending) row sat first
    and masked the later paid row, so the board showed paid trips as unpaid.
    """

    @classmethod
    def setUpTestData(cls):
        origin = Location.objects.create(name="MCO Airport")
        dest = Location.objects.create(name="Disney World")
        route = Route.objects.create(origin=origin, destination=dest)
        vehicle = Vehicle.objects.create(
            vehicle_type="suv", capacity=6, luggage_capacity=6
        )
        cls.rate = Rate.objects.create(
            vehicle=vehicle, route=route,
            oneway_price=Decimal("140"), round_trip_price=Decimal("275"),
        )

    def setUp(self):
        post_save.disconnect(reservation_saved, sender=Reservation)
        self.addCleanup(
            lambda: post_save.connect(reservation_saved, sender=Reservation)
        )
        self.customer = Customer.objects.create(
            first_name="Pay", last_name="Status",
            email="paystatus@example.com", phone_number="555-000-1111",
        )
        self.reservation = Reservation.objects.create(
            trip_type="oneway", customer=self.customer, rate=self.rate,
            base_price=Decimal("275"), total_price=Decimal("275"),
            status="confirmed",
        )

    def _payment(self, status, amount="275.00"):
        from payment.models import Payment
        return Payment.objects.create(
            reservation=self.reservation, customer=self.customer,
            amount=Decimal(amount), status=status,
        )

    def _status(self):
        # payment_status is a cached_property — read it on a fresh instance.
        return Reservation.objects.get(pk=self.reservation.pk).payment_status

    def test_paid_wins_over_earlier_card_saved_row(self):
        self._payment("card_saved")   # booking-time save-card row, lower pk
        self._payment("paid")         # the actual charge, later row
        self.assertEqual(self._status(), "paid")

    def test_paid_wins_over_earlier_pending_row(self):
        self._payment("pending")
        self._payment("paid")
        self.assertEqual(self._status(), "paid")

    def test_card_saved_wins_over_pending(self):
        self._payment("pending")
        self._payment("card_saved")
        self.assertEqual(self._status(), "card_saved")

    def test_single_card_saved_unchanged(self):
        self._payment("card_saved")
        self.assertEqual(self._status(), "card_saved")

    def test_no_payments_is_unpaid(self):
        self.assertEqual(self._status(), "unpaid")

    def test_only_unrecognized_statuses_fall_back_to_failed(self):
        self._payment("refunded")
        self.assertEqual(self._status(), "failed")

    def test_prefetched_payments_path_matches(self):
        self._payment("card_saved")
        self._payment("paid")
        res = (
            Reservation.objects.prefetch_related("payments")
            .get(pk=self.reservation.pk)
        )
        self.assertEqual(res.payment_status, "paid")
