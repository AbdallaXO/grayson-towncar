"""Schedule board — two drivers splitting one car (2026-09-28).

Run with:  ENABLE_DEBUG_TOOLBAR=0 python manage.py test dispatching.tests_board_car_share

With 20+ rows a shared car was just two "#17" chips a few lines apart, and a job
could go to one driver while the other had the car. What must hold:
  * PAIRED: both rows of a shared unit carry `share` — a chip naming the other
    driver ("Leo till ~1:30p" / "Cara from ~4:10p") and a "Car with …" band where
    the other driver has the car. Whoever has the car first sits on top.
  * GRADED: a trip that runs into the partner's, or sits between their first and
    last pickup, is a CLASH (red chip + flag on the trip); drive-out / drive-back
    margins touching is only TIGHT (amber). A clean handoff is neither.
  * A driver on their own car gets none of it.
  * DRAG: dropping a job on a driver whose car is out with the partner then comes
    back from check-feasibility as a warning — the row goes amber and the Assign
    Anyway question names the shared car. A job clear of the partner does not.
"""
from datetime import time, timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from dispatching.scheduler import preload_timing_cache
from drivers.models import Driver, DriverVehicleAssignment, FleetVehicle
from rates.models import Location, Rate, Route, Vehicle
from reservations.models import Customer, Leg, Reservation

DAY = timezone.localdate() + timedelta(days=5)


class _SharedCarFixture(TestCase):
    """Zed has unit 17 in the morning, Abe after — named so the alphabet would
    put the afternoon driver on top. Pat drives unit 8 alone."""

    @classmethod
    def setUpTestData(cls):
        preload_timing_cache()
        cls.vehicle = Vehicle.objects.create(vehicle_type="suv", capacity=6, luggage_capacity=6)
        route = Route.objects.create(origin=Location.objects.create(name="MCO"),
                                     destination=Location.objects.create(name="Disney"),
                                     inhouse_base_pay=Decimal("50.00"))
        cls.route = route
        cls.rate = Rate.objects.create(vehicle=cls.vehicle, route=route,
                                       oneway_price=Decimal("100.00"),
                                       round_trip_price=Decimal("180.00"))
        cls.customer = Customer.objects.create(first_name="Jo", last_name="Guest",
                                               email="jo@example.com",
                                               phone_number="5551234567")
        unit17 = FleetVehicle.objects.create(vehicle_number="17", vehicle_type=cls.vehicle,
                                             year=2022, make="Chevrolet", model="Suburban")
        unit8 = FleetVehicle.objects.create(vehicle_number="8", vehicle_type=cls.vehicle,
                                            year=2023, make="Chevrolet", model="Tahoe")
        cls.zed = cls._driver("cs_zed", "Zed", unit17)
        cls.abe = cls._driver("cs_abe", "Abe", unit17)
        cls.pat = cls._driver("cs_pat", "Pat", unit8)
        cls.staff = User.objects.create_user("cs_staff", password="x", is_staff=True)

    @classmethod
    def _driver(cls, username, first, unit):
        d = Driver.objects.create(
            profile=User.objects.create_user(username, first_name=first), driver_type="inhouse")
        DriverVehicleAssignment.objects.create(driver=d, date=DAY, vehicle=unit)
        return d

    def setUp(self):
        self.client.force_login(self.staff)

    def _leg(self, hh, mm=0, driver=None):
        res = Reservation.objects.create(
            trip_type="one-way", customer=self.customer, rate=self.rate,
            vehicle=self.vehicle, base_price=Decimal("100.00"), total_price=Decimal("100.00"))
        return Leg.objects.create(
            reservation=res, pickup_date=DAY, pickup_time=time(hh, mm),
            pickup_location="MCO", dropoff_location="Disney", route=self.route,
            status="confirmed", driver=driver)

    def _rows(self):
        r = self.client.get(reverse("schedule_board"), {"date": DAY.isoformat()})
        self.assertEqual(r.status_code, 200)
        return r, {row["driver"].id: row for row in r.context["inhouse_timeline"]}


class BoardSharedCarTests(_SharedCarFixture):

    def _clean_day(self):
        self._leg(7, driver=self.zed)
        self._leg(12, driver=self.zed)
        self._leg(17, driver=self.abe)

    def test_both_rows_name_the_other_driver_and_draw_their_time(self):
        self._clean_day()
        resp, rows = self._rows()
        zed, abe = rows[self.zed.id]["share"], rows[self.abe.id]["share"]
        self.assertEqual(zed["unit"], "#17")
        self.assertIn("Abe from ~", zed["chip"])
        self.assertIn("Zed till ~", abe["chip"])
        self.assertEqual((zed["level"], abe["level"]), ("", ""))
        self.assertEqual([b["label"] for b in abe["bands"]], ["Car with Zed"])
        self.assertEqual([b["label"] for b in zed["bands"]], ["Car with Abe"])
        self.assertContains(resp, 'class="tl-share-band', count=2)
        self.assertContains(resp, 'class="share-chip')
        self.assertNotContains(resp, 'class="tl-share-flag"')

    def test_whoever_has_the_car_first_sits_on_top(self):
        self._clean_day()
        _, rows = self._rows()
        order = [d for d in rows if d in (self.zed.id, self.abe.id)]
        self.assertEqual(order, [self.zed.id, self.abe.id])   # Zed (AM) above Abe
        self.assertTrue(rows[self.zed.id]["share_first"])
        self.assertTrue(rows[self.abe.id]["share_last"])

    def test_a_driver_on_their_own_car_gets_nothing(self):
        self._clean_day()
        self._leg(9, driver=self.pat)
        _, rows = self._rows()
        self.assertNotIn("share", rows[self.pat.id])

    @staticmethod
    def _flagged(row):
        return [s.leg_id for s in row["schedule"].slots if getattr(s, "share_clash", False)]

    def test_the_trip_that_splits_the_partners_day_is_the_one_marked(self):
        # Zed has the car 7 AM – 1 PM; Abe's 9 AM would make it change hands twice.
        # Only that trip goes red — the dispatcher sees which job to move.
        self._leg(7, driver=self.zed)
        self._leg(12, driver=self.zed)
        self._leg(13, driver=self.zed)
        self._leg(17, driver=self.abe)
        mid = self._leg(9, driver=self.abe)
        resp, rows = self._rows()
        abe, zed = rows[self.abe.id], rows[self.zed.id]
        self.assertEqual(abe["share"]["level"], "clash")
        self.assertEqual(self._flagged(abe), [mid.id])
        self.assertEqual(self._flagged(zed), [])
        self.assertEqual(zed["share"]["level"], "")
        self.assertIn("Move this trip.", next(
            s.share_clash_title for s in abe["schedule"].slots if s.leg_id == mid.id))
        self.assertContains(resp, 'class="tl-share-flag"')
        self.assertContains(resp, '<b class="share-state">Clash</b>')

    def test_a_true_tie_marks_both_and_says_either_can_move(self):
        self._clean_day()
        mid = self._leg(9, driver=self.abe)      # Zed 7 · Abe 9 · Zed 12 · Abe 5 PM
        _, rows = self._rows()
        self.assertEqual(self._flagged(rows[self.abe.id]), [mid.id])
        self.assertEqual(len(self._flagged(rows[self.zed.id])), 1)
        self.assertIn("or the other driver's trip", next(
            s.share_clash_title for s in rows[self.abe.id]["schedule"].slots
            if s.leg_id == mid.id))

    def test_a_close_handoff_is_only_tight(self):
        self._leg(7, driver=self.zed)
        self._leg(12, driver=self.zed)
        self._leg(14, driver=self.abe)           # after Zed's last run, inside its drive back
        _, rows = self._rows()
        abe = rows[self.abe.id]["share"]
        self.assertEqual(abe["level"], "tight")
        self.assertIn("Zed till ~", abe["chip"])
        self.assertFalse(any(getattr(s, "share_clash", False)
                             for s in rows[self.abe.id]["schedule"].slots))

    def test_a_partner_with_no_jobs_yet_is_still_named(self):
        self._leg(7, driver=self.zed)
        _, rows = self._rows()
        self.assertEqual(rows[self.zed.id]["share"]["chip"], "with Abe")
        self.assertEqual(rows[self.zed.id]["share"]["bands"], [])
        self.assertEqual([b["label"] for b in rows[self.abe.id]["share"]["bands"]],
                         ["Car with Zed"])


class DragOntoASharedCarTests(_SharedCarFixture):

    def _feas(self, leg, driver):
        r = self.client.get(reverse("check_driver_feasibility"),
                            {"leg_id": leg.id, "driver_id": driver.id})
        self.assertEqual(r.status_code, 200, r.content)
        return r.json()

    def test_dropping_a_job_while_the_partner_has_the_car_warns(self):
        self._leg(7, driver=self.zed)
        self._leg(12, driver=self.zed)
        job = self._leg(9)
        data = self._feas(job, self.abe)
        self.assertTrue(any("Shared car #17" in w for w in data["warnings"]), data)

    def test_a_job_clear_of_the_partner_does_not(self):
        self._leg(7, driver=self.zed)
        self._leg(12, driver=self.zed)
        job = self._leg(20)
        data = self._feas(job, self.abe)
        self.assertFalse(any("Shared car" in w for w in data["warnings"]), data)

    def test_a_close_hand_over_asks_without_calling_it_impossible(self):
        self._leg(7, driver=self.zed)
        self._leg(12, driver=self.zed)
        job = self._leg(14)
        texts = [w for w in self._feas(job, self.abe)["warnings"] if "Shared car" in w]
        self.assertEqual(len(texts), 1, texts)
        self.assertIn("close hand-over", texts[0])
        self.assertNotIn("can't", texts[0])

    def test_a_finished_run_still_counts(self):
        # Zed's 7 AM is done, his 6 PM isn't; noon on Abe would still put the car
        # back and forth — completed trips are facts about who had it.
        done = self._leg(7, driver=self.zed)
        done.status = "completed"
        done.save(update_fields=["status"])
        self._leg(18, driver=self.zed)
        job = self._leg(12)
        texts = [w for w in self._feas(job, self.abe)["warnings"] if "Shared car" in w]
        self.assertTrue(texts and "change hands twice" in texts[0], texts)

    def test_the_switch_turns_it_off(self):
        from dispatching.models import SchedulerSettings
        cfg = SchedulerSettings.get_settings()
        cfg.manual_assign_warnings = False
        cfg.save()
        SchedulerSettings.get_settings()   # reload the cached singleton
        self._leg(7, driver=self.zed)
        self._leg(12, driver=self.zed)
        job = self._leg(9)
        try:
            data = self._feas(job, self.abe)
        finally:
            cfg.manual_assign_warnings = True
            cfg.save()
        self.assertFalse(any("Shared car" in w for w in data["warnings"]), data)

    def test_a_driver_on_their_own_car_is_never_warned(self):
        self._leg(9, driver=self.zed)
        job = self._leg(9, 15)
        data = self._feas(job, self.pat)
        self.assertFalse(any("Shared car" in w for w in data["warnings"]), data)


class UnitDayReadingTests(TestCase):
    """car_share.read_unit_day — the one reading the board and the drop share."""

    @staticmethod
    def _t(leg_id, did, hh, mm=0, clear=None, cat=("resort", "resort")):
        from datetime import datetime
        return {"leg_id": leg_id, "did": did, "pick": datetime(2026, 5, 1, hh, mm),
                "clear": clear, "pickup_category": cat[0], "dropoff_category": cat[1],
                "movable": True}

    def test_a_long_last_trip_keeps_the_car_out_until_it_clears(self):
        from datetime import datetime
        from dispatching.car_share import read_unit_day
        late_clear = datetime(2026, 5, 1, 14, 30)       # well past the table's tail
        day = read_unit_day([self._t(1, "A", 12, clear=late_clear), self._t(2, "B", 18)])
        (start, end, ids), = day["runs"]["A"]
        self.assertEqual(ids, [1])
        self.assertGreaterEqual(end, late_clear)

    def test_a_trip_starting_while_the_other_is_running_is_the_clash(self):
        from datetime import datetime
        from dispatching.car_share import read_unit_day
        day = read_unit_day([self._t(1, "A", 12, clear=datetime(2026, 5, 1, 14, 0)),
                             self._t(2, "B", 13, 30)])
        self.assertEqual(day["grade"], {1: "", 2: "clash"})
        self.assertEqual(day["why"][2], "overlap")
