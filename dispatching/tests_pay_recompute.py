"""Tests for driver pay following its inputs instead of being written once.

Run with:  ENABLE_DEBUG_TOOLBAR=0 ./manage.py test dispatching.tests_pay_recompute

Covers:
  * The night bonus moving when a pickup crosses 22:01 / 05:59 AFTER pay exists.
  * pay_manually_set protecting a hand-typed amount from every automatic path.
  * The booking-rate fallback being gone: an unmatched trip stays unpriced
    rather than being quietly priced off the reservation's rate.
  * An address edit re-pricing against the NEW route, and leaving the leg alone
    when the new addresses match nothing.
  * reset_schedule clearing pay so the next driver cannot inherit it.
  * exclude_from_payroll keeping an account off the Driver Payments page.
"""
import json
from datetime import date, time
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from drivers.models import Driver
from rates.models import Location, Rate, Route, Vehicle, Zone, ZoneRate
from reservations.models import Customer, Leg, LegStop, Reservation


def _make_driver(username, night_bonus=Decimal("10.00"), driver_type="inhouse"):
    user = User.objects.create_user(username=username, first_name=username.title())
    return Driver.objects.create(
        profile=user, driver_type=driver_type, night_bonus=night_bonus
    )


class _PayFixtureMixin:
    @classmethod
    def setUpTestData(cls):
        cls.vehicle = Vehicle.objects.create(
            vehicle_type="sedan", capacity=4, luggage_capacity=4
        )
        # Migration 0026 ships real zones; these tests build their own so they
        # describe the rules rather than the current prices.
        cls.local = Zone.objects.create(name="Test Local", sort_order=1)
        cls.outer = Zone.objects.create(name="Test Outer", sort_order=2)
        cls.mco = Location.objects.create(name="MCO", pay_zone=cls.local)
        cls.disney = Location.objects.create(name="Disney", pay_zone=cls.local)
        cls.port = Location.objects.create(name="Port Canaveral", pay_zone=cls.outer)
        # In a zone, and deliberately no Route row to anywhere: priced by zone alone.
        cls.idrive = Location.objects.create(name="I-Drive", pay_zone=cls.local)
        # No zone at all — nobody has agreed a price, so it must stay unpriced.
        cls.unzoned = Location.objects.create(name="Nowheresville", pay_zone=None)
        ZoneRate.objects.create(zone_a=cls.local, zone_b=cls.local, inhouse_base_pay=Decimal("25.00"))
        ZoneRate.objects.create(zone_a=cls.local, zone_b=cls.outer, inhouse_base_pay=Decimal("40.00"))
        ZoneRate.objects.create(zone_a=cls.outer, zone_b=cls.outer, inhouse_base_pay=Decimal("50.00"))
        cls.route = Route.objects.create(
            origin=cls.mco, destination=cls.disney, inhouse_base_pay=Decimal("25.00")
        )
        cls.port_route = Route.objects.create(
            origin=cls.mco, destination=cls.port, inhouse_base_pay=Decimal("40.00")
        )
        cls.rate = Rate.objects.create(
            vehicle=cls.vehicle, route=cls.route,
            oneway_price=Decimal("100.00"), round_trip_price=Decimal("180.00"),
        )
        cls.customer = Customer.objects.create(
            first_name="Jane", last_name="Roe", email="jane@example.com",
            phone_number="5550001111",
        )

    def _res(self, gratuity=Decimal("0.00"), base=Decimal("180.00")):
        return Reservation.objects.create(
            trip_type="one-way", customer=self.customer, rate=self.rate,
            vehicle=self.vehicle, base_price=base, total_price=base,
            gratuity_amount=gratuity,
        )

    def _leg(self, res, driver=None, **kw):
        defaults = dict(
            reservation=res, pickup_date=date(2026, 6, 1), pickup_time=time(9, 0),
            pickup_location="MCO", dropoff_location="Disney", status="confirmed",
        )
        defaults.update(kw)
        leg = Leg.objects.create(**defaults)
        if driver is not None:
            leg.driver = driver
            leg.save()
        return leg


class NightBonusFollowsRetimeTests(_PayFixtureMixin, TestCase):
    def test_moving_into_the_night_window_adds_the_bonus(self):
        drv = _make_driver("nightin", night_bonus=Decimal("20.00"))
        leg = self._leg(self._res(), driver=drv, pickup_time=time(21, 0))
        self.assertEqual(leg.driver_base_pay, Decimal("25.00"))
        self.assertIn(leg.driver_additional, (None, Decimal("0.00")))

        leg.pickup_time = time(23, 30)
        leg.save(update_fields=["pickup_time"])

        leg.refresh_from_db()
        self.assertEqual(leg.driver_additional, Decimal("20.00"))
        self.assertEqual(leg.driver_base_pay, Decimal("25.00"))
        self.assertEqual(leg.driver_pay_amount, Decimal("45.00"))

    def test_moving_out_of_the_night_window_removes_the_bonus(self):
        drv = _make_driver("nightout", night_bonus=Decimal("10.00"))
        leg = self._leg(self._res(), driver=drv, pickup_time=time(23, 30))
        self.assertEqual(leg.driver_additional, Decimal("10.00"))

        leg.pickup_time = time(9, 15)
        leg.save(update_fields=["pickup_time"])

        leg.refresh_from_db()
        self.assertEqual(leg.driver_additional, Decimal("0.00"))
        self.assertEqual(leg.driver_pay_amount, Decimal("25.00"))

    def test_wait_time_in_the_same_field_survives_the_bonus_change(self):
        """driver_additional is a mixed bucket, so the move must be a delta."""
        drv = _make_driver("mixed", night_bonus=Decimal("10.00"))
        leg = self._leg(self._res(), driver=drv, pickup_time=time(9, 0))
        # Dispatcher paid $35 for an extra stop; not a manual override of the rate.
        Leg.objects.filter(pk=leg.pk).update(driver_additional=Decimal("35.00"))
        leg.refresh_from_db()

        leg.pickup_time = time(23, 0)
        leg.save(update_fields=["pickup_time"])

        leg.refresh_from_db()
        self.assertEqual(leg.driver_additional, Decimal("45.00"))

    def test_retime_inside_one_window_changes_nothing(self):
        drv = _make_driver("samewindow")
        leg = self._leg(self._res(), driver=drv, pickup_time=time(9, 0))
        before = leg.driver_pay_amount

        leg.pickup_time = time(10, 30)
        leg.save(update_fields=["pickup_time"])

        leg.refresh_from_db()
        self.assertEqual(leg.driver_pay_amount, before)
        self.assertIn(leg.driver_additional, (None, Decimal("0.00")))

    def test_bonus_never_goes_negative_when_the_rate_changed_mid_flight(self):
        drv = _make_driver("raised", night_bonus=Decimal("10.00"))
        leg = self._leg(self._res(), driver=drv, pickup_time=time(23, 30))
        self.assertEqual(leg.driver_additional, Decimal("10.00"))

        # Bonus raised after the leg was priced; moving out of the window would
        # otherwise claw back $20 against the $10 that was actually applied.
        drv.night_bonus = Decimal("20.00")
        drv.save(update_fields=["night_bonus"])
        leg = Leg.objects.get(pk=leg.pk)

        leg.pickup_time = time(9, 0)
        leg.save(update_fields=["pickup_time"])

        leg.refresh_from_db()
        self.assertEqual(leg.driver_additional, Decimal("0.00"))

    def test_paid_leg_is_never_repriced(self):
        drv = _make_driver("alreadypaid")
        leg = self._leg(self._res(), driver=drv, pickup_time=time(9, 0))
        Leg.objects.filter(pk=leg.pk).update(payment_status="paid")
        leg = Leg.objects.get(pk=leg.pk)

        leg.pickup_time = time(23, 30)
        leg.save(update_fields=["pickup_time"])

        leg.refresh_from_db()
        self.assertIn(leg.driver_additional, (None, Decimal("0.00")))

    def test_canceled_leg_is_never_repriced(self):
        drv = _make_driver("canceledleg")
        leg = self._leg(self._res(), driver=drv, pickup_time=time(9, 0))
        Leg.objects.filter(pk=leg.pk).update(payment_status="canceled")
        leg = Leg.objects.get(pk=leg.pk)

        leg.pickup_time = time(23, 30)
        leg.save(update_fields=["pickup_time"])

        leg.refresh_from_db()
        self.assertIn(leg.driver_additional, (None, Decimal("0.00")))

    def test_unpriced_leg_does_not_get_a_bonus_without_a_base(self):
        """A bonus alone would shut the auto-fill guard with base pay still NULL."""
        drv = _make_driver("noroute")
        leg = self._leg(
            self._res(), driver=drv, pickup_time=time(9, 0),
            pickup_location="Clermont", dropoff_location="Nowhere",
        )
        self.assertIsNone(leg.driver_base_pay)

        leg.pickup_time = time(23, 30)
        leg.save(update_fields=["pickup_time"])

        leg.refresh_from_db()
        self.assertIsNone(leg.driver_base_pay)
        self.assertIsNone(leg.driver_additional)


class ManualPayIsProtectedTests(_PayFixtureMixin, TestCase):
    def test_retime_leaves_a_hand_typed_amount_alone(self):
        drv = _make_driver("typed", night_bonus=Decimal("10.00"))
        leg = self._leg(self._res(), driver=drv, pickup_time=time(9, 0))
        Leg.objects.filter(pk=leg.pk).update(
            driver_base_pay=Decimal("80.00"), driver_additional=Decimal("0.00"),
            driver_pay_amount=Decimal("80.00"), pay_manually_set=True,
        )
        leg = Leg.objects.get(pk=leg.pk)

        leg.pickup_time = time(23, 30)
        leg.save(update_fields=["pickup_time"])

        leg.refresh_from_db()
        self.assertEqual(leg.driver_base_pay, Decimal("80.00"))
        self.assertEqual(leg.driver_additional, Decimal("0.00"))

    def test_the_pay_endpoint_marks_the_leg(self):
        staff = User.objects.create_user("payroll", password="x", is_staff=True)
        drv = _make_driver("endpoint")
        leg = self._leg(self._res(), driver=drv)
        self.client.force_login(staff)

        resp = self.client.post(
            reverse("update_driver_pay_amount"),
            data=json.dumps({
                "leg_id": leg.id, "driver_base_pay": "55.00",
                "driver_gratuity": "0", "driver_additional": "0",
            }),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200)
        leg.refresh_from_db()
        self.assertTrue(leg.pay_manually_set)
        self.assertEqual(leg.driver_base_pay, Decimal("55.00"))

    def test_changing_driver_clears_the_manual_flag(self):
        drv_a = _make_driver("driver_a")
        drv_b = _make_driver("driver_b")
        leg = self._leg(self._res(), driver=drv_a)
        Leg.objects.filter(pk=leg.pk).update(
            driver_base_pay=Decimal("90.00"), pay_manually_set=True
        )
        leg = Leg.objects.get(pk=leg.pk)

        leg.driver = drv_b
        leg.save()

        leg.refresh_from_db()
        self.assertFalse(leg.pay_manually_set)
        self.assertEqual(leg.driver_base_pay, Decimal("25.00"))


class NoBookingRateFallbackTests(_PayFixtureMixin, TestCase):
    def test_unmatched_addresses_leave_the_leg_unpriced(self):
        drv = _make_driver("clermont")
        res = self._res()  # its rate points at the MCO -> Disney route, $25
        leg = self._leg(
            res, driver=drv,
            pickup_location="1189 Esperanza Ridge Rd Clermont, FL 34715",
            dropoff_location="Somewhere Unmatched",
        )
        self.assertIsNone(leg.route_id)
        self.assertIsNone(leg.driver_base_pay)

    def test_matching_addresses_still_price_normally(self):
        drv = _make_driver("normal")
        leg = self._leg(self._res(), driver=drv)
        self.assertEqual(leg.route_id, self.route.id)
        self.assertEqual(leg.driver_base_pay, Decimal("25.00"))

    def test_recalculate_leaves_an_unpriceable_leg_untouched(self):
        staff = User.objects.create_user("recalc", password="x", is_staff=True)
        drv = _make_driver("recalcdrv")
        leg = self._leg(
            self._res(), driver=drv,
            pickup_location="Clermont FL", dropoff_location="Unmatched Place",
        )
        self.client.force_login(staff)

        resp = self.client.post(
            reverse("recalculate_driver_pay"),
            data=json.dumps({"leg_ids": [leg.id]}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIn(leg.id, resp.json()["needs_pricing"])
        leg.refresh_from_db()
        self.assertIsNone(leg.route_id)
        self.assertIsNone(leg.driver_base_pay)

    def test_recalculate_refuses_a_paid_leg(self):
        staff = User.objects.create_user("recalcpaid", password="x", is_staff=True)
        drv = _make_driver("paiddrv")
        leg = self._leg(self._res(), driver=drv)
        Leg.objects.filter(pk=leg.pk).update(
            payment_status="paid", driver_base_pay=Decimal("99.00")
        )
        self.client.force_login(staff)

        self.client.post(
            reverse("recalculate_driver_pay"),
            data=json.dumps({"leg_ids": [leg.id], "force": True}),
            content_type="application/json",
        )
        leg.refresh_from_db()
        self.assertEqual(leg.driver_base_pay, Decimal("99.00"))


class AddressChangeRepricesTests(_PayFixtureMixin, TestCase):
    def test_moving_the_dropoff_to_another_route_reprices(self):
        drv = _make_driver("moved")
        leg = self._leg(self._res(), driver=drv)
        self.assertEqual(leg.driver_base_pay, Decimal("25.00"))

        leg.dropoff_location = "Port Canaveral"
        leg.save(update_fields=["dropoff_location"])

        leg.refresh_from_db()
        self.assertEqual(leg.route_id, self.port_route.id)
        self.assertEqual(leg.driver_base_pay, Decimal("40.00"))

    def test_moving_to_unmatched_addresses_keeps_the_existing_route(self):
        drv = _make_driver("stillmatched")
        leg = self._leg(self._res(), driver=drv)

        leg.dropoff_location = "Some Unmatched Venue"
        leg.save(update_fields=["dropoff_location"])

        leg.refresh_from_db()
        self.assertEqual(leg.route_id, self.route.id)
        self.assertEqual(leg.driver_base_pay, Decimal("25.00"))


class ResetSchedulePayTests(_PayFixtureMixin, TestCase):
    def test_next_driver_does_not_inherit_the_previous_rate(self):
        """reset_schedule bypasses save(), so it has to clear pay itself."""
        drv_a = _make_driver("first", night_bonus=Decimal("20.00"))
        drv_b = _make_driver("second", night_bonus=Decimal("10.00"))
        leg = self._leg(self._res(), driver=drv_a, pickup_time=time(23, 30))
        self.assertEqual(leg.driver_additional, Decimal("20.00"))

        # What reset_schedule does to an unpaid leg.
        Leg.objects.filter(pk=leg.pk, payment_status="unpaid").update(
            driver_base_pay=None, driver_gratuity=None, driver_additional=None,
            driver_pay_amount=None, pay_manually_set=False,
        )
        Leg.objects.filter(pk=leg.pk).update(driver=None)

        leg = Leg.objects.get(pk=leg.pk)
        leg.driver = drv_b
        leg.save()

        leg.refresh_from_db()
        self.assertEqual(leg.driver_base_pay, Decimal("25.00"))
        self.assertEqual(leg.driver_additional, Decimal("10.00"))


class ExcludeFromPayrollTests(_PayFixtureMixin, TestCase):
    def test_excluded_driver_is_absent_from_the_payments_page(self):
        staff = User.objects.create_user("payroll2", password="x", is_staff=True)
        founder = _make_driver("founder")
        founder.exclude_from_payroll = True
        founder.save(update_fields=["exclude_from_payroll"])
        chauffeur = _make_driver("chauffeur")
        self._leg(self._res(), driver=founder, status="completed")
        self._leg(self._res(), driver=chauffeur, status="completed")

        self.client.force_login(staff)
        resp = self.client.get(reverse("driver_payment_management"))
        self.assertEqual(resp.status_code, 200)
        ids = {d.id for d in resp.context["drivers"]}
        self.assertNotIn(founder.id, ids)
        self.assertIn(chauffeur.id, ids)


class PaymentsPageSurfacesTests(_PayFixtureMixin, TestCase):
    """The detail page has to actually render the new flags and the stop line."""

    def _staff(self):
        user = User.objects.create_user("pagestaff", password="x", is_staff=True)
        self.client.force_login(user)
        return user

    def test_detail_page_shows_the_extra_stop_and_its_guest_fee(self):
        self._staff()
        drv = _make_driver("stopdrv")
        leg = self._leg(self._res(), driver=drv, status="completed")
        LegStop.objects.create(
            leg=leg, sequence=0, location_text="Publix on Universal Blvd",
            stop_type="stop", duration_minutes=20, extra_fee=Decimal("40.00"),
        )

        resp = self.client.get(
            reverse("driver_payment_management"), {"driver": drv.id}
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.content.decode()
        self.assertIn("Publix on Universal Blvd", body)
        self.assertIn("guest paid $40.00", body)
        self.assertIn("Extra stops:", body)

    def test_detail_page_flags_a_leg_with_no_route(self):
        self._staff()
        drv = _make_driver("nopricedrv")
        self._leg(
            self._res(), driver=drv, status="completed",
            pickup_location="Clermont FL", dropoff_location="Unmatched Place",
        )

        resp = self.client.get(
            reverse("driver_payment_management"), {"driver": drv.id}
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context["needs_pricing_count"], 1)
        self.assertIn("NEEDS PRICE", resp.content.decode())

    def test_detail_page_flags_an_over_attributed_tip(self):
        self._staff()
        drv = _make_driver("tipdrv")
        res = self._res(gratuity=Decimal("40.00"))
        sibling = self._leg(res, status="completed")
        Leg.objects.filter(pk=sibling.pk).update(driver_gratuity=Decimal("0.00"))
        leg = self._leg(res, driver=drv, status="completed")

        # The split hands this leg the whole tip because the sibling reads as pinned.
        leg.refresh_from_db()
        self.assertEqual(leg.driver_gratuity, Decimal("40.00"))

        resp = self.client.get(
            reverse("driver_payment_management"), {"driver": drv.id}
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIn("CHECK TIP", resp.content.decode())


class ZonePricingTests(_PayFixtureMixin, TestCase):
    """A zone is a price tier. Any two endpoints in known zones can be priced,
    whether or not anyone ever entered that pair as a Route."""

    def test_local_pair_with_no_route_row_is_priced_from_the_zone(self):
        drv = _make_driver("zonelocal")
        leg = self._leg(
            self._res(), driver=drv,
            pickup_location="MCO", dropoff_location="I-Drive",
        )
        self.assertIsNone(leg.route_id)  # no Route row exists for this pair
        self.assertEqual(leg.driver_base_pay, Decimal("25.00"))

    def test_local_to_outer_with_no_route_row_pays_the_outer_rate(self):
        drv = _make_driver("zoneouter")
        leg = self._leg(
            self._res(), driver=drv,
            pickup_location="I-Drive", dropoff_location="Port Canaveral",
        )
        self.assertIsNone(leg.route_id)
        self.assertEqual(leg.driver_base_pay, Decimal("40.00"))

    def test_hotel_to_hotel_inside_one_zone_is_priced(self):
        drv = _make_driver("hoteltohotel")
        leg = self._leg(
            self._res(), driver=drv,
            pickup_location="Disney", dropoff_location="I-Drive",
        )
        self.assertEqual(leg.driver_base_pay, Decimal("25.00"))

    def test_an_explicit_route_overrides_its_zone(self):
        """Championsgate is $35 even though both ends are Zone 1."""
        gate = Location.objects.create(name="Championsgate", pay_zone=self.local)
        Route.objects.create(
            origin=self.mco, destination=gate, inhouse_base_pay=Decimal("35.00")
        )
        drv = _make_driver("gatedrv")
        leg = self._leg(
            self._res(), driver=drv,
            pickup_location="MCO", dropoff_location="Championsgate",
        )
        self.assertEqual(leg.driver_base_pay, Decimal("35.00"))

    def test_a_place_with_no_zone_stays_unpriced(self):
        drv = _make_driver("tampadrv")
        leg = self._leg(
            self._res(), driver=drv,
            pickup_location="Disney", dropoff_location="Nowheresville",
        )
        self.assertIsNone(leg.driver_base_pay)

    def test_zones_do_not_price_affiliates(self):
        """Affiliates are paid negotiated card rates, not zone rates."""
        aff = _make_driver("affzone", driver_type="affiliate")
        leg = self._leg(
            self._res(), driver=aff,
            pickup_location="MCO", dropoff_location="I-Drive",
        )
        self.assertIsNone(leg.driver_base_pay)

    def test_changing_the_zone_rate_changes_future_pricing(self):
        ZoneRate.objects.filter(zone_a=self.local, zone_b=self.local).update(
            inhouse_base_pay=Decimal("28.00")
        )
        drv = _make_driver("newrate")
        leg = self._leg(
            self._res(), driver=drv,
            pickup_location="MCO", dropoff_location="I-Drive",
        )
        self.assertEqual(leg.driver_base_pay, Decimal("28.00"))

    def test_address_edit_reprices_across_zones(self):
        drv = _make_driver("crosszone")
        leg = self._leg(
            self._res(), driver=drv,
            pickup_location="MCO", dropoff_location="I-Drive",
        )
        self.assertEqual(leg.driver_base_pay, Decimal("25.00"))

        leg.dropoff_location = "Port Canaveral"
        leg.save(update_fields=["dropoff_location"])

        leg.refresh_from_db()
        self.assertEqual(leg.driver_base_pay, Decimal("40.00"))


class RatesPageTests(_PayFixtureMixin, TestCase):
    """The Pay Rates page is the whole zone UI, so it has to work without the
    Django admin: set a zone price, add a zone, move a place, add a place."""

    def setUp(self):
        self.staff = User.objects.create_user("rates_staff", password="x", is_staff=True)
        self.client.force_login(self.staff)

    def test_page_renders_the_zone_grid_and_places(self):
        resp = self.client.get(reverse("driver_pay_rates"))
        self.assertEqual(resp.status_code, 200)
        body = resp.content.decode()
        self.assertIn("Zone prices", body)
        self.assertIn("Places &amp; their zones", body)
        self.assertIn("Test Local", body)
        # Each unordered pair is offered once, so the same trip cannot be given
        # two different prices from the two halves of the grid. (Migration 0026
        # ships real zones alongside the test ones, so count from the table.)
        n = Zone.objects.count()
        self.assertEqual(body.count('class="zone-cell-input'), n * (n + 1) // 2)

    def test_setting_a_zone_price_saves_it(self):
        resp = self.client.post(
            reverse("update_zone_rate"),
            data=json.dumps({
                "zone_a": self.local.id, "zone_b": self.outer.id, "base_pay": "45.00",
            }),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(
            ZoneRate.pay_for(self.local.id, self.outer.id), Decimal("45.00")
        )

    def test_clearing_a_zone_price_unprices_that_pairing(self):
        self.client.post(
            reverse("update_zone_rate"),
            data=json.dumps({"zone_a": self.local.id, "zone_b": self.outer.id, "base_pay": ""}),
            content_type="application/json",
        )
        self.assertIsNone(ZoneRate.pay_for(self.local.id, self.outer.id))

    def test_the_price_is_the_same_whichever_way_round_it_is_sent(self):
        self.client.post(
            reverse("update_zone_rate"),
            data=json.dumps({
                "zone_a": self.outer.id, "zone_b": self.local.id, "base_pay": "44.00",
            }),
            content_type="application/json",
        )
        self.assertEqual(ZoneRate.pay_for(self.local.id, self.outer.id), Decimal("44.00"))
        self.assertEqual(ZoneRate.objects.filter(
            zone_a__in=[self.local, self.outer], zone_b__in=[self.local, self.outer]
        ).exclude(zone_a=self.local, zone_b=self.local).exclude(
            zone_a=self.outer, zone_b=self.outer
        ).count(), 1)

    def test_adding_a_zone(self):
        resp = self.client.post(
            reverse("save_zone"),
            data=json.dumps({"name": "Clermont / west", "description": "west of Disney"}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(Zone.objects.filter(name="Clermont / west").exists())

    def test_a_duplicate_zone_name_is_refused(self):
        resp = self.client.post(
            reverse("save_zone"),
            data=json.dumps({"name": "test local"}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 400)

    def test_a_zone_with_places_in_it_cannot_be_deleted(self):
        resp = self.client.post(
            reverse("delete_zone"),
            data=json.dumps({"zone_id": self.local.id}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 400)
        self.assertTrue(Zone.objects.filter(id=self.local.id).exists())

    def test_moving_a_place_to_another_zone_changes_what_new_trips_pay(self):
        drv = _make_driver("zonemove")
        first = self._leg(
            self._res(), driver=drv,
            pickup_location="MCO", dropoff_location="I-Drive",
        )
        self.assertEqual(first.driver_base_pay, Decimal("25.00"))

        self.client.post(
            reverse("save_place"),
            data=json.dumps({"location_id": self.idrive.id, "zone_id": self.outer.id}),
            content_type="application/json",
        )

        later = self._leg(
            self._res(), driver=drv,
            pickup_location="MCO", dropoff_location="I-Drive",
        )
        self.assertEqual(later.driver_base_pay, Decimal("40.00"))
        # The already-priced leg is left exactly as it was.
        first.refresh_from_db()
        self.assertEqual(first.driver_base_pay, Decimal("25.00"))

    def test_adding_an_alias_makes_an_address_price(self):
        drv = _make_driver("aliasdrv")
        before = self._leg(
            self._res(), driver=drv,
            pickup_location="MCO", dropoff_location="Waldorf Astoria Orlando",
        )
        self.assertIsNone(before.driver_base_pay)

        self.client.post(
            reverse("save_place"),
            data=json.dumps({"location_id": self.disney.id, "aliases": "Waldorf Astoria"}),
            content_type="application/json",
        )

        after = self._leg(
            self._res(), driver=drv,
            pickup_location="MCO", dropoff_location="Waldorf Astoria Orlando",
        )
        self.assertEqual(after.driver_base_pay, Decimal("25.00"))

    def test_adding_a_place(self):
        resp = self.client.post(
            reverse("save_place"),
            data=json.dumps({
                "name": "Clermont", "zone_id": self.outer.id, "aliases": "Clermont FL",
            }),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200)
        loc = Location.objects.get(name="Clermont")
        self.assertEqual(loc.pay_zone_id, self.outer.id)

    def test_a_non_staff_user_cannot_change_prices(self):
        self.client.logout()
        plain = User.objects.create_user("nobody", password="x")
        self.client.force_login(plain)
        resp = self.client.post(
            reverse("update_zone_rate"),
            data=json.dumps({"zone_a": self.local.id, "zone_b": self.local.id, "base_pay": "1"}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(ZoneRate.pay_for(self.local.id, self.local.id), Decimal("25.00"))


class PayrollRunScreenTests(_PayFixtureMixin, TestCase):
    """The run screen has to show every in-house driver at once, put the ones
    needing a decision first, and pay one driver at a time from the row."""

    def setUp(self):
        self.staff = User.objects.create_user("runstaff", password="x", is_staff=True)
        self.client.force_login(self.staff)

    def _completed(self, driver, **kw):
        kw.setdefault("status", "completed")
        kw.setdefault("pickup_date", date(2026, 6, 1))
        return self._leg(self._res(), driver=driver, **kw)

    def test_lists_every_inhouse_driver_with_unpaid_work(self):
        a = _make_driver("run_a")
        b = _make_driver("run_b")
        self._completed(a)
        self._completed(b)

        resp = self.client.get(reverse("payroll_run"), {"to_date": "2026-06-30"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context["driver_count"], 2)
        self.assertEqual(resp.context["leg_total"], 2)
        self.assertEqual(resp.context["money_total"], Decimal("50.00"))

    def test_drivers_needing_a_decision_come_first(self):
        clean = _make_driver("run_clean")
        flagged = _make_driver("run_flagged")
        self._completed(clean)
        # No zone on either end, so nothing can price it.
        self._completed(
            flagged, pickup_location="Nowheresville", dropoff_location="Nowheresville",
        )

        resp = self.client.get(reverse("payroll_run"), {"to_date": "2026-06-30"})
        rows = resp.context["rows"]
        self.assertEqual(rows[0]["driver"].id, flagged.id)
        self.assertEqual(rows[0]["flag_count"], 1)
        self.assertEqual(rows[1]["flag_count"], 0)
        self.assertEqual(resp.context["needs_look_count"], 1)
        self.assertEqual(resp.context["ready_count"], 1)

    def test_an_unpaid_extra_stop_is_flagged(self):
        drv = _make_driver("run_stop")
        leg = self._completed(drv)
        LegStop.objects.create(
            leg=leg, sequence=0, location_text="Publix", stop_type="stop",
            duration_minutes=20, extra_fee=Decimal("40.00"),
        )
        resp = self.client.get(reverse("payroll_run"), {"to_date": "2026-06-30"})
        row = resp.context["rows"][0]
        self.assertEqual(row["flag_count"], 1)
        self.assertIn("extra stop", " ".join(row["flagged"][0].flag_labels))

    def test_excluded_and_affiliate_drivers_never_appear(self):
        founder = _make_driver("run_founder")
        founder.exclude_from_payroll = True
        founder.save(update_fields=["exclude_from_payroll"])
        affiliate = _make_driver("run_aff", driver_type="affiliate")
        self._completed(founder)
        self._completed(affiliate)

        resp = self.client.get(reverse("payroll_run"), {"to_date": "2026-06-30"})
        self.assertEqual(resp.context["driver_count"], 0)

    def test_the_to_date_bounds_the_run(self):
        drv = _make_driver("run_dates")
        self._completed(drv, pickup_date=date(2026, 6, 1))
        self._completed(drv, pickup_date=date(2026, 7, 1))

        resp = self.client.get(reverse("payroll_run"), {"to_date": "2026-06-15"})
        self.assertEqual(resp.context["leg_total"], 1)

    def test_in_progress_legs_are_not_part_of_a_run(self):
        drv = _make_driver("run_open")
        self._completed(drv)
        self._leg(self._res(), driver=drv, status="in-progress")

        resp = self.client.get(reverse("payroll_run"), {"to_date": "2026-06-30"})
        self.assertEqual(resp.context["leg_total"], 1)

    def test_old_work_is_called_out(self):
        drv = _make_driver("run_stale")
        self._completed(drv, pickup_date=date(2026, 5, 1))
        resp = self.client.get(reverse("payroll_run"), {"to_date": "2026-06-30"})
        self.assertTrue(resp.context["rows"][0]["stale"])

    def test_paying_one_driver_leaves_the_others_alone(self):
        a = _make_driver("run_pay_a")
        b = _make_driver("run_pay_b")
        leg_a = self._completed(a)
        leg_b = self._completed(b)

        resp = self.client.post(
            reverse("process_driver_payment"),
            data=json.dumps({"driver_id": a.id, "date_to": "2026-06-30", "send_email": False}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200)
        leg_a.refresh_from_db()
        leg_b.refresh_from_db()
        self.assertEqual(leg_a.payment_status, "paid")
        self.assertEqual(leg_b.payment_status, "unpaid")

        # And the row drops out of the next load of the run.
        again = self.client.get(reverse("payroll_run"), {"to_date": "2026-06-30"})
        self.assertEqual(again.context["driver_count"], 1)
        self.assertEqual(again.context["rows"][0]["driver"].id, b.id)

    def test_a_non_staff_user_cannot_open_the_run(self):
        self.client.logout()
        self.client.force_login(User.objects.create_user("run_nobody", password="x"))
        resp = self.client.get(reverse("payroll_run"))
        self.assertEqual(resp.status_code, 302)


class ReadyMeansCheckedTests(_PayFixtureMixin, TestCase):
    """"Ready" has to mean the amount was checked, not merely that one exists.

    Before this, a trip carrying any number at all read as ready — which is how
    a Clermont run to the cruise port sat at $25 and looked fine.
    """

    def setUp(self):
        self.staff = User.objects.create_user("readystaff", password="x", is_staff=True)
        self.client.force_login(self.staff)

    def _run(self):
        return self.client.get(reverse("payroll_run"), {"to_date": "2026-06-30"})

    def test_a_wrong_amount_is_caught(self):
        drv = _make_driver("mismatch")
        leg = self._leg(self._res(), driver=drv, status="completed")
        # A number nobody typed and the rates do not support.
        Leg.objects.filter(pk=leg.pk).update(
            driver_base_pay=Decimal("250.00"), driver_pay_amount=Decimal("250.00")
        )

        row = self._run().context["rows"][0]
        self.assertEqual(row["flag_count"], 1)
        why = " ".join(row["flagged"][0].flag_labels)
        self.assertIn("$250.00", why)
        self.assertIn("$25.00", why)

    def test_an_amount_a_person_typed_is_left_alone(self):
        drv = _make_driver("typed_ok")
        leg = self._leg(self._res(), driver=drv, status="completed")
        Leg.objects.filter(pk=leg.pk).update(
            driver_base_pay=Decimal("250.00"),
            driver_pay_amount=Decimal("250.00"),
            pay_manually_set=True,
        )

        row = self._run().context["rows"][0]
        self.assertEqual(row["flag_count"], 0)

    def test_a_night_pickup_missing_its_bonus_is_caught(self):
        drv = _make_driver("nightless", night_bonus=Decimal("20.00"))
        leg = self._leg(
            self._res(), driver=drv, status="completed", pickup_time=time(23, 30)
        )
        # Priced as if it were a day trip.
        Leg.objects.filter(pk=leg.pk).update(
            driver_additional=Decimal("0.00"), driver_pay_amount=Decimal("25.00")
        )

        row = self._run().context["rows"][0]
        self.assertEqual(row["flag_count"], 1)
        self.assertIn("bonus", " ".join(row["flagged"][0].flag_labels))

    def test_a_correctly_priced_night_trip_is_ready(self):
        drv = _make_driver("nightok", night_bonus=Decimal("20.00"))
        self._leg(self._res(), driver=drv, status="completed", pickup_time=time(23, 30))

        resp = self._run()
        self.assertEqual(resp.context["rows"][0]["flag_count"], 0)
        self.assertEqual(resp.context["ready_count"], 1)

    def test_an_ordinary_correctly_priced_trip_is_ready(self):
        drv = _make_driver("plain_ok")
        self._leg(self._res(), driver=drv, status="completed")
        self.assertEqual(self._run().context["rows"][0]["flag_count"], 0)

    def test_the_page_spells_out_what_ready_checked(self):
        drv = _make_driver("meaning")
        self._leg(self._res(), driver=drv, status="completed")
        body = self._run().content.decode()
        self.assertIn("the amount matches the rates", body)
        self.assertIn("does <strong>not</strong> check", body)


class ReviewEveryTripTests(_PayFixtureMixin, TestCase):
    """One pass over the whole run, for a person and for Cowork."""

    def setUp(self):
        self.staff = User.objects.create_user("reviewstaff", password="x", is_staff=True)
        self.client.force_login(self.staff)

    def test_review_mode_lists_every_trip_not_just_the_flagged(self):
        drv = _make_driver("review_all")
        for _ in range(3):
            self._leg(self._res(), driver=drv, status="completed")

        # The class name also appears in the stylesheet, so assert on the markup
        # that only the review table emits.
        normal = self.client.get(reverse("payroll_run"), {"to_date": "2026-06-30"})
        self.assertFalse(normal.context["show_all"])
        self.assertNotIn('class="cell-input pay-base', normal.content.decode())

        every = self.client.get(
            reverse("payroll_run"), {"to_date": "2026-06-30", "show": "all"}
        )
        self.assertTrue(every.context["show_all"])
        body = every.content.decode()
        self.assertIn('<table class="trip-table">', body)
        self.assertEqual(body.count('class="cell-input pay-base'), 3)

    def test_correcting_an_amount_keeps_the_guest_tip(self):
        """The pay endpoint rewrites all three fields, so the tip must ride along."""
        drv = _make_driver("keeps_tip")
        res = self._res(gratuity=Decimal("40.00"))
        leg = self._leg(res, driver=drv, status="completed")
        leg.refresh_from_db()
        self.assertEqual(leg.driver_gratuity, Decimal("40.00"))

        resp = self.client.post(
            reverse("update_driver_pay_amount"),
            data=json.dumps({
                "leg_id": leg.id,
                "driver_base_pay": "55.00",
                "driver_gratuity": str(leg.driver_gratuity),
                "driver_additional": "0",
            }),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200)
        leg.refresh_from_db()
        self.assertEqual(leg.driver_base_pay, Decimal("55.00"))
        self.assertEqual(leg.driver_gratuity, Decimal("40.00"))
        self.assertEqual(leg.driver_pay_amount, Decimal("95.00"))
        self.assertTrue(leg.pay_manually_set)

    def test_review_mode_marks_which_amounts_a_person_typed(self):
        drv = _make_driver("typed_tag")
        leg = self._leg(self._res(), driver=drv, status="completed")
        Leg.objects.filter(pk=leg.pk).update(pay_manually_set=True)

        body = self.client.get(
            reverse("payroll_run"), {"to_date": "2026-06-30", "show": "all"}
        ).content.decode()
        self.assertIn("typed by hand", body)

    def test_review_mode_shows_what_the_rates_say_when_they_disagree(self):
        drv = _make_driver("shows_rates")
        leg = self._leg(self._res(), driver=drv, status="completed")
        Leg.objects.filter(pk=leg.pk).update(driver_base_pay=Decimal("250.00"))

        body = self.client.get(
            reverse("payroll_run"), {"to_date": "2026-06-30", "show": "all"}
        ).content.decode()
        self.assertIn("rates: $25.00", body)


class ReviewNotesAndWordingTests(_PayFixtureMixin, TestCase):
    """The notes usually explain an amount better than the flags do, and the
    button has to say what it actually does."""

    def setUp(self):
        self.staff = User.objects.create_user("notestaff", password="x", is_staff=True)
        self.client.force_login(self.staff)

    def _review(self):
        return self.client.get(
            reverse("payroll_run"), {"to_date": "2026-06-30", "show": "all"}
        ).content.decode()

    def test_all_three_kinds_of_note_are_shown(self):
        drv = _make_driver("noted")
        res = self._res()
        res.special_requests = "Guest asked for a booster seat"
        res.save(update_fields=["special_requests"])
        leg = self._leg(res, driver=drv, status="completed")
        Leg.objects.filter(pk=leg.pk).update(
            private_notes="Waited 40 min at the curb",
            driver_notes="Called guest twice",
        )

        body = self._review()
        self.assertIn("Waited 40 min at the curb", body)
        self.assertIn("Called guest twice", body)
        self.assertIn("Guest asked for a booster seat", body)

    def test_a_trip_with_no_notes_renders_nothing_extra(self):
        drv = _make_driver("unnoted")
        self._leg(self._res(), driver=drv, status="completed")
        self.assertNotIn('class="note-line"', self._review())

    def test_the_button_does_not_call_it_paying(self):
        """It writes a statement. No card is charged and no transfer happens."""
        drv = _make_driver("wording")
        self._leg(self._res(), driver=drv, status="completed")

        body = self.client.get(
            reverse("payroll_run"), {"to_date": "2026-06-30"}
        ).content.decode()
        self.assertIn("Record statement", body)
        self.assertIn("no money moves", body.lower())
        self.assertNotIn(">Pay<", body)
