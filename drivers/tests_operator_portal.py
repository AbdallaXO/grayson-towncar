"""Tests for the operator portal — affiliates who re-dispatch our jobs.

Covers the four things that would actually hurt if they broke: an operator
lands in the right portal, the copy block carries the whole job (car seats
included), a decline really does hand the leg back to our board, and one
operator can never touch another's legs.

Run with:  ./manage.py test drivers.tests_operator_portal
"""
import json
from datetime import time, timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from drivers.models import Driver
from drivers.operator_jobs import build_job_fields, build_job_text
from rates.models import Location, Rate, Route, Vehicle
from reservations.models import Customer, Leg, LegKeoi, LegStatus, Reservation


def _make_operator(username, first="Acme", last="Limo"):
    user = User.objects.create_user(username=username, first_name=first, last_name=last)
    return Driver.objects.create(profile=user, driver_type="affiliate", portal_role="operator")


def _make_chauffeur(username):
    user = User.objects.create_user(username=username, first_name="Reg", last_name="Driver")
    return Driver.objects.create(profile=user, driver_type="affiliate", portal_role="driver")


def _bootstrap_reservation(**kwargs):
    vehicle = Vehicle.objects.create(vehicle_type="suv", capacity=6, luggage_capacity=6)
    route = Route.objects.create(
        origin=Location.objects.create(name="MCO"),
        destination=Location.objects.create(name="Disney"),
    )
    rate = Rate.objects.create(
        vehicle=vehicle, route=route,
        oneway_price=Decimal("100.00"), round_trip_price=Decimal("180.00"),
    )
    customer = Customer.objects.create(
        first_name="Jane", last_name="Guest",
        email="jane@example.com", phone_number="4075550134",
    )
    defaults = dict(
        trip_type="one-way", customer=customer, rate=rate, vehicle=vehicle,
        base_price=Decimal("100"), total_price=Decimal("100"),
        passenger_count=3, luggage_count=4,
    )
    defaults.update(kwargs)
    return Reservation.objects.create(**defaults)


def _make_leg(reservation, driver, *, pickup_date, status="in-progress", **kwargs):
    return Leg.objects.create(
        reservation=reservation, driver=driver,
        pickup_date=pickup_date, pickup_time=kwargs.pop("pickup_time", time(9, 0)),
        pickup_location="MCO", dropoff_location="Disney World",
        status=status, **kwargs
    )


@override_settings(GOOGLE_MAPS_API_KEY="")
class RoutingTests(TestCase):
    """An operator must land on the job board, a chauffeur must not."""

    @classmethod
    def setUpTestData(cls):
        cls.operator = _make_operator("acme_ops")
        cls.chauffeur = _make_chauffeur("reg_chauffeur")

    def test_operator_redirected_from_chauffeur_pages(self):
        self.client.force_login(self.operator.profile)
        for name in ("schedule", "drivers_dashboard", "completed_trips"):
            resp = self.client.get(reverse(name))
            self.assertRedirects(resp, reverse("operator_board"), msg_prefix=name)

    def test_chauffeur_keeps_the_chauffeur_portal(self):
        self.client.force_login(self.chauffeur.profile)
        resp = self.client.get(reverse("schedule"))
        self.assertEqual(resp.status_code, 200)

    def test_chauffeur_hitting_operator_board_is_sent_home(self):
        self.client.force_login(self.chauffeur.profile)
        resp = self.client.get(reverse("operator_board"))
        self.assertRedirects(resp, reverse("schedule"))

    def test_board_requires_login(self):
        resp = self.client.get(reverse("operator_board"))
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/login", resp["Location"])


@override_settings(GOOGLE_MAPS_API_KEY="")
class JobTextTests(TestCase):
    """The copy block is the feature. It has to be complete and money-free."""

    @classmethod
    def setUpTestData(cls):
        cls.operator = _make_operator("copy_ops")
        cls.reservation = _bootstrap_reservation(
            need_carseats=True, rf_carseats=1, booster_seats=2, extra_boosters=1,
            special_requests="Meet inside baggage claim",
        )
        cls.leg = _make_leg(cls.reservation, cls.operator, pickup_date=timezone.localdate())

    def test_carries_the_whole_job(self):
        text = build_job_text(self.leg)
        for expected in [
            f"Confirmation: {self.reservation.display_number}",
            "Passenger: Jane Guest",
            "Phone: (407) 555-0134",
            "Pickup: MCO",
            "Dropoff: Disney World",
            "Passengers: 3",
            "Notes: Meet inside baggage claim",
        ]:
            self.assertIn(expected, text)

    def test_car_seats_are_in_the_copy_block(self):
        """The detail that gets missed on a farm-out, including the 'extra' seats
        the reservation-level formatter drops."""
        text = build_job_text(self.leg)
        self.assertIn("Car seats: 1 Rear-Facing, 2 Booster, 1 Extra Booster", text)

    def test_leg_override_beats_the_reservation(self):
        """Seats edited on ONE leg of a round trip must copy that leg's numbers."""
        self.leg.rf_carseats = 0
        self.leg.ff_carseats = 3
        self.leg.booster_seats = 0
        self.leg.extra_boosters = 0
        self.leg.save()
        self.assertIn("Car seats: 3 Forward-Facing", build_job_text(self.leg))

    def test_no_money_anywhere(self):
        text = build_job_text(self.leg)
        self.assertNotIn("$", text)
        for banned in ("price", "rate", "pay", "total"):
            self.assertNotIn(banned, text.lower())

    def test_blank_fields_are_dropped(self):
        labels = [label for label, _ in build_job_fields(self.leg)]
        self.assertNotIn("Cruise", labels)   # not a cruise job
        self.assertNotIn("Flight", labels)   # no flight attached

    def test_board_renders_the_car_seats(self):
        self.client.force_login(self.operator.profile)
        resp = self.client.get(reverse("operator_board"))
        self.assertContains(resp, "1 Rear-Facing, 2 Booster, 1 Extra Booster")


@override_settings(GOOGLE_MAPS_API_KEY="")
class AcceptDeclineTests(TestCase):
    def setUp(self):
        self.operator = _make_operator("ad_ops")
        self.reservation = _bootstrap_reservation()
        self.leg = _make_leg(
            self.reservation, self.operator,
            pickup_date=timezone.localdate() + timedelta(days=2),
        )
        self.client.force_login(self.operator.profile)

    def _post(self, name, body=None):
        return self.client.post(
            reverse(name, args=[self.leg.id]),
            data=json.dumps(body or {}), content_type="application/json",
        )

    def test_accept_confirms_and_stamps(self):
        resp = self._post("operator_accept")
        self.assertEqual(resp.status_code, 200)
        self.leg.refresh_from_db()
        self.assertEqual(self.leg.status, "confirmed")
        self.assertIsNotNone(self.leg.operator_accepted_at)
        self.assertTrue(
            LegStatus.objects.filter(leg=self.leg, notes="Accepted by operator").exists()
        )

    def test_decline_hands_the_leg_back(self):
        resp = self._post("operator_decline", {"reason": "No car available"})
        self.assertEqual(resp.status_code, 200)
        self.leg.refresh_from_db()
        self.assertIsNone(self.leg.driver)                      # back on our board
        self.assertEqual(self.leg.operator_declined_by, self.operator)
        self.assertEqual(self.leg.operator_decline_reason, "No car available")
        self.assertIsNotNone(self.leg.operator_declined_at)

    def test_decline_raises_a_watch_flag(self):
        self._post("operator_decline", {"reason": "Already booked"})
        flag = LegKeoi.objects.filter(leg=self.leg, closed_at__isnull=True).first()
        self.assertIsNotNone(flag, "declined leg must show on the board's needs-attention surface")
        self.assertIn("declined this farm-out", flag.description)
        self.assertEqual(flag.operational_status, LegKeoi.OperationalStatus.NEEDS_ATTENTION)

    def test_decline_needs_a_reason(self):
        resp = self._post("operator_decline", {"reason": "   "})
        self.assertEqual(resp.status_code, 400)
        self.leg.refresh_from_db()
        self.assertEqual(self.leg.driver, self.operator)

    def test_decline_survives_a_missing_keoi(self):
        """A flag that cannot be written must not roll back the unassign."""
        LegKeoi.objects.create(
            leg=self.leg, category=LegKeoi.Category.OTHER,
            description="already watching", created_by=self.operator.profile,
        )
        resp = self._post("operator_decline", {"reason": "No car"})
        self.assertEqual(resp.status_code, 200)
        self.leg.refresh_from_db()
        self.assertIsNone(self.leg.driver)
        self.assertEqual(LegKeoi.objects.filter(leg=self.leg, closed_at__isnull=True).count(), 1)

    def test_accept_clears_an_earlier_decline(self):
        self.leg.operator_declined_by = self.operator
        self.leg.operator_declined_at = timezone.now()
        self.leg.operator_decline_reason = "changed my mind"
        self.leg.save()
        self._post("operator_accept")
        self.leg.refresh_from_db()
        self.assertIsNone(self.leg.operator_declined_at)
        self.assertEqual(self.leg.operator_decline_reason, "")

    def test_completed_job_cannot_be_declined(self):
        self.leg.status = "completed"
        self.leg.save()
        resp = self._post("operator_decline", {"reason": "too late"})
        self.assertEqual(resp.status_code, 400)


@override_settings(GOOGLE_MAPS_API_KEY="")
class OperatorDriverTests(TestCase):
    def setUp(self):
        self.operator = _make_operator("sub_ops")
        self.reservation = _bootstrap_reservation()
        self.leg = _make_leg(self.reservation, self.operator, pickup_date=timezone.localdate())
        self.client.force_login(self.operator.profile)

    def _assign(self, **body):
        return self.client.post(
            reverse("operator_assign_driver", args=[self.leg.id]),
            data=json.dumps(body), content_type="application/json",
        )

    def test_name_and_phone_saved(self):
        resp = self._assign(name="Carlos M", phone="4075559090")
        self.assertEqual(resp.status_code, 200)
        self.leg.refresh_from_db()
        self.assertEqual(self.leg.operator_driver_name, "Carlos M")
        self.assertEqual(self.leg.operator_driver_phone, "4075559090")

    def test_name_alone_is_enough(self):
        """The user asked for phone to be optional — a name-only save must stick."""
        self._assign(name="Carlos M")
        self.leg.refresh_from_db()
        self.assertEqual(self.leg.operator_driver_name, "Carlos M")
        self.assertEqual(self.leg.operator_driver_phone, "")

    def test_clearing_the_name_clears_the_phone(self):
        self._assign(name="Carlos M", phone="4075559090")
        self._assign(name="", phone="4075559090")
        self.leg.refresh_from_db()
        self.assertEqual(self.leg.operator_driver_name, "")
        self.assertEqual(self.leg.operator_driver_phone, "")

    def test_reassigning_the_leg_clears_the_operators_driver(self):
        """Their chauffeur must not follow the leg to another company."""
        self._assign(name="Carlos M", phone="4075559090")
        other = _make_operator("other_ops", first="Beta")
        self.leg.refresh_from_db()
        self.leg.driver = other
        self.leg.save()
        self.leg.refresh_from_db()
        self.assertEqual(self.leg.operator_driver_name, "")
        self.assertEqual(self.leg.operator_driver_phone, "")

    def test_past_drivers_are_offered_back(self):
        from drivers.operator_views import recent_operator_drivers
        self._assign(name="Carlos M", phone="4075559090")
        known = recent_operator_drivers(self.operator)
        self.assertEqual(known, [{"name": "Carlos M", "phone": "4075559090"}])


@override_settings(GOOGLE_MAPS_API_KEY="")
class IsolationTests(TestCase):
    """One operator must never see or touch another's work."""

    def setUp(self):
        self.mine = _make_operator("mine_ops", first="Mine")
        self.theirs = _make_operator("theirs_ops", first="Theirs")
        self.reservation = _bootstrap_reservation()
        self.their_leg = _make_leg(self.reservation, self.theirs, pickup_date=timezone.localdate())
        self.client.force_login(self.mine.profile)

    def test_cannot_accept_someone_elses_leg(self):
        resp = self.client.post(
            reverse("operator_accept", args=[self.their_leg.id]),
            data="{}", content_type="application/json",
        )
        self.assertEqual(resp.status_code, 404)

    def test_cannot_decline_someone_elses_leg(self):
        resp = self.client.post(
            reverse("operator_decline", args=[self.their_leg.id]),
            data=json.dumps({"reason": "nope"}), content_type="application/json",
        )
        self.assertEqual(resp.status_code, 404)
        self.their_leg.refresh_from_db()
        self.assertEqual(self.their_leg.driver, self.theirs)

    def test_cannot_name_a_driver_on_someone_elses_leg(self):
        resp = self.client.post(
            reverse("operator_assign_driver", args=[self.their_leg.id]),
            data=json.dumps({"name": "Hijack"}), content_type="application/json",
        )
        self.assertEqual(resp.status_code, 404)

    def test_board_shows_only_my_jobs(self):
        mine = _make_leg(self.reservation, self.mine, pickup_date=timezone.localdate(),
                         pickup_time=time(11, 30))
        resp = self.client.get(reverse("operator_board"))
        self.assertContains(resp, f'data-leg-id="{mine.id}"')
        self.assertNotContains(resp, f'data-leg-id="{self.their_leg.id}"')

    def test_chauffeur_cannot_use_operator_endpoints(self):
        chauffeur = _make_chauffeur("nosy_chauffeur")
        leg = _make_leg(self.reservation, chauffeur, pickup_date=timezone.localdate())
        self.client.force_login(chauffeur.profile)
        resp = self.client.post(
            reverse("operator_accept", args=[leg.id]),
            data="{}", content_type="application/json",
        )
        self.assertEqual(resp.status_code, 403)


@override_settings(GOOGLE_MAPS_API_KEY="")
class ResponseQueueTests(TestCase):
    def setUp(self):
        self.operator = _make_operator("queue_ops")
        self.reservation = _bootstrap_reservation()
        self.client.force_login(self.operator.profile)

    def test_future_unanswered_job_is_queued_even_though_it_is_not_today(self):
        far = _make_leg(
            self.reservation, self.operator,
            pickup_date=timezone.localdate() + timedelta(days=21),
        )
        resp = self.client.get(reverse("operator_board"))
        self.assertContains(resp, "Needs your answer")
        self.assertContains(resp, f'data-leg-id="{far.id}"')

    def test_accepted_job_leaves_the_queue(self):
        leg = _make_leg(
            self.reservation, self.operator,
            pickup_date=timezone.localdate() + timedelta(days=3),
        )
        self.client.post(reverse("operator_accept", args=[leg.id]),
                         data="{}", content_type="application/json")
        resp = self.client.get(reverse("operator_board"))
        self.assertNotContains(resp, "Needs your answer")

    def test_cancelled_job_never_reaches_the_operator(self):
        cancelled = _make_leg(self.reservation, self.operator,
                              pickup_date=timezone.localdate(), status="cancelled")
        live = _make_leg(self.reservation, self.operator,
                         pickup_date=timezone.localdate(), pickup_time=time(14, 0))
        resp = self.client.get(reverse("operator_board"))
        # Assert on the rendered page, not resp.context: creating a Reservation
        # kicks off a confirmation email in a background thread, and that
        # template ALSO binds a `legs` variable. resp.context is a ContextList
        # that returns whichever rendered first, so it is a coin flip here.
        self.assertNotContains(resp, f'data-leg-id="{cancelled.id}"')
        self.assertContains(resp, f'data-leg-id="{live.id}"')

    def test_unstaffed_count_flags_jobs_without_a_named_driver(self):
        _make_leg(self.reservation, self.operator, pickup_date=timezone.localdate())
        resp = self.client.get(reverse("operator_board"))
        self.assertContains(resp, "1 without a driver")


@override_settings(GOOGLE_MAPS_API_KEY="")
class NavigationTests(TestCase):
    """The operator has a portal, not a single screen: today, ahead, and done."""

    def setUp(self):
        self.operator = _make_operator("nav_ops")
        self.reservation = _bootstrap_reservation()
        self.client.force_login(self.operator.profile)

    def test_every_page_carries_the_nav(self):
        for name in ("operator_board", "operator_upcoming", "operator_completed"):
            resp = self.client.get(reverse(name))
            self.assertEqual(resp.status_code, 200, msg=name)
            self.assertContains(resp, "Today's Jobs", msg_prefix=name)
            self.assertContains(resp, "Upcoming", msg_prefix=name)
            self.assertContains(resp, "Completed", msg_prefix=name)

    def test_upcoming_groups_future_jobs_by_day(self):
        soon = _make_leg(self.reservation, self.operator,
                         pickup_date=timezone.localdate() + timedelta(days=1))
        later = _make_leg(self.reservation, self.operator,
                          pickup_date=timezone.localdate() + timedelta(days=9))
        resp = self.client.get(reverse("operator_upcoming"))
        self.assertContains(resp, f'data-leg-id="{soon.id}"')
        self.assertContains(resp, f'data-leg-id="{later.id}"')
        self.assertEqual(len(resp.context["days"]), 2)

    def test_upcoming_hides_past_and_completed(self):
        past = _make_leg(self.reservation, self.operator,
                         pickup_date=timezone.localdate() - timedelta(days=3))
        done = _make_leg(self.reservation, self.operator,
                         pickup_date=timezone.localdate() + timedelta(days=1),
                         status="completed")
        resp = self.client.get(reverse("operator_upcoming"))
        self.assertNotContains(resp, f'data-leg-id="{past.id}"')
        self.assertNotContains(resp, f'data-leg-id="{done.id}"')

    def test_completed_lists_finished_jobs_only(self):
        done = _make_leg(self.reservation, self.operator,
                         pickup_date=timezone.localdate() - timedelta(days=2),
                         status="completed")
        live = _make_leg(self.reservation, self.operator,
                         pickup_date=timezone.localdate(), status="confirmed")
        resp = self.client.get(reverse("operator_completed"))
        self.assertContains(resp, f'data-leg-id="{done.id}"')
        self.assertNotContains(resp, f'data-leg-id="{live.id}"')

    def test_completed_is_read_only(self):
        """A finished job is a record — no status dropdown, no accept/decline."""
        _make_leg(self.reservation, self.operator,
                  pickup_date=timezone.localdate() - timedelta(days=2),
                  status="completed")
        resp = self.client.get(reverse("operator_completed"))
        # Assert on the rendered CONTROLS, not the class names — the shared base
        # template's JS mentions every one of those selectors on every page.
        self.assertNotContains(resp, "<select")
        self.assertNotContains(resp, "Update status")
        self.assertNotContains(resp, "Accept job")
        self.assertNotContains(resp, "Change driver")
        self.assertNotContains(resp, "Add driver")

    def test_completed_still_lets_them_copy_the_job(self):
        done = _make_leg(self.reservation, self.operator,
                         pickup_date=timezone.localdate() - timedelta(days=2),
                         status="completed")
        resp = self.client.get(reverse("operator_completed"))
        self.assertContains(resp, "Copy whole job")

    def test_unanswered_count_follows_them_across_pages(self):
        _make_leg(self.reservation, self.operator,
                  pickup_date=timezone.localdate() + timedelta(days=4))
        for name in ("operator_board", "operator_upcoming", "operator_completed"):
            resp = self.client.get(reverse(name))
            self.assertEqual(resp.context["pending_count"], 1, msg=name)

    def test_chauffeur_cannot_reach_the_new_pages(self):
        chauffeur = _make_chauffeur("nav_chauffeur")
        self.client.force_login(chauffeur.profile)
        for name in ("operator_upcoming", "operator_completed"):
            resp = self.client.get(reverse(name))
            self.assertRedirects(resp, reverse("schedule"), msg_prefix=name)
