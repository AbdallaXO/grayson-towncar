"""Day Setup tests — roster gate, vehicle matching, and the atomic apply endpoint.

Design record: docs/scheduler-automation/auto-assign-hour-balancing-design.md PART 4.
"""
import json
from datetime import date, timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from dispatching.day_setup import suggest_day_setup
from drivers.models import Driver, DriverVehicleAssignment, DriverWeeklySchedule, FleetVehicle
from rates.models import Vehicle

User = get_user_model()
TARGET = date(2026, 6, 10)


def _mk_driver(username, first_name="", certified=None, **kw):
    u = User.objects.create_user(username=username, password="x", first_name=first_name)
    d = Driver.objects.create(profile=u, driver_type="inhouse", is_active=True, **kw)
    if certified:
        d.certified_vehicle_types.add(certified)
    return d


class DaySetupSuggestTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.vt_suv = Vehicle.objects.create(vehicle_type="suv", capacity=6, luggage_capacity=6)
        cls.vt_town = Vehicle.objects.create(vehicle_type="towncar", capacity=4, luggage_capacity=4)
        cls.vt_van14 = Vehicle.objects.create(vehicle_type="Van(14 Pax)", capacity=14,
                                              luggage_capacity=14, requires_certification=True)
        cls.u004 = FleetVehicle.objects.create(vehicle_number="004", vehicle_type=cls.vt_van14,
                                               year=2022, make="Mercedes", model="Sprinter")
        cls.u007 = FleetVehicle.objects.create(vehicle_number="007", vehicle_type=cls.vt_suv,
                                               year=2023, make="Chevy", model="Suburban")
        cls.u013 = FleetVehicle.objects.create(vehicle_number="013", vehicle_type=cls.vt_town,
                                               year=2021, make="Lincoln", model="Towncar")
        cls.rob = _mk_driver("rob", certified=cls.vt_van14)
        cls.dave = _mk_driver("dave")
        cls.newguy = _mk_driver("newguy")

    def _history(self, driver, unit, days, end=TARGET):
        """`days` rows on consecutive dates STRICTLY BEFORE `end`."""
        for i in range(1, days + 1):
            DriverVehicleAssignment.objects.create(
                driver=driver, date=end - timedelta(days=i), vehicle=unit)

    def test_off_driver_never_suggested(self):
        # Founder's hard gate: schedule says OFF => off group, unchecked, no vehicle.
        DriverWeeklySchedule.objects.create(
            driver=self.rob, day_of_week=TARGET.weekday(), is_available=False,
            shift_type="full_day", start_hour=0, end_hour=23)
        out = suggest_day_setup(TARGET)
        row = next(r for r in out["rows"] if r["driver_id"] == self.rob.id)
        self.assertEqual(row["group"], "off")
        self.assertFalse(row["checked"])
        self.assertIsNone(row["vehicle_id"])

    def test_demo_account_excluded(self):
        demo = _mk_driver("priyad", first_name="Priya (demo)")
        out = suggest_day_setup(TARGET)
        self.assertNotIn(demo.id, [r["driver_id"] for r in out["rows"]])

    def test_dedicated_lock_gets_his_car(self):
        self._history(self.rob, self.u004, 20)
        self._history(self.dave, self.u007, 5)
        out = suggest_day_setup(TARGET)
        row = next(r for r in out["rows"] if r["driver_id"] == self.rob.id)
        self.assertEqual(row["vehicle_id"], self.u004.id)
        self.assertIn("usual unit", row["reason"])

    def test_cert_gate_blocks_van14(self):
        # dave (uncertified) must never be offered the Sprinter even when it's the only unit.
        self.u007.delete(); self.u013.delete()
        out = suggest_day_setup(TARGET)
        row = next(r for r in out["rows"] if r["driver_id"] == self.dave.id)
        self.assertNotEqual(row["vehicle_id"], self.u004.id)
        self.assertTrue(any("No free unit" in w for w in out["warnings"]) or row["vehicle_id"] is None)

    def test_locked_rows_untouched(self):
        DriverVehicleAssignment.objects.create(driver=self.dave, date=TARGET, vehicle=self.u007)
        out = suggest_day_setup(TARGET)
        row = next(r for r in out["rows"] if r["driver_id"] == self.dave.id)
        self.assertEqual(row["group"], "locked")
        self.assertEqual(row["vehicle_id"], self.u007.id)
        # the locked unit is not offered to anyone else
        self.assertNotIn(self.u007.id, [u["id"] for u in out["free_units"]])
        for r in out["rows"]:
            if r["driver_id"] != self.dave.id:
                self.assertNotEqual(r["vehicle_id"], self.u007.id)

    def test_history_strictly_before_target(self):
        # Founder pre-builds future days — future rows must NOT create a dedicated lock.
        for i in range(0, 20):
            DriverVehicleAssignment.objects.create(
                driver=self.rob, date=TARGET + timedelta(days=1 + i), vehicle=self.u004)
        out = suggest_day_setup(TARGET)
        row = next(r for r in out["rows"] if r["driver_id"] == self.rob.id)
        self.assertNotIn("usual unit", row["reason"] or "")

    def test_swap_callout_when_usual_unit_held(self):
        self._history(self.rob, self.u004, 20)
        other = _mk_driver("holder", certified=self.vt_van14)
        DriverVehicleAssignment.objects.create(driver=other, date=TARGET, vehicle=self.u004)
        out = suggest_day_setup(TARGET)
        self.assertTrue(any("usually drives" in s for s in out["swaps"]))

    def test_deterministic(self):
        self._history(self.rob, self.u004, 10)
        self._history(self.dave, self.u007, 4)
        self.assertEqual(suggest_day_setup(TARGET), suggest_day_setup(TARGET))

    def test_regular_gets_his_car_back_after_day_off(self):
        # Founder scenario: george (a #004 regular) was off yesterday; dave drove #004
        # yesterday. Today both work: george gets #004 back, dave gets another unit, and
        # a handback callout explains it.
        self._history(self.rob, self.u004, 20, end=TARGET - timedelta(days=1))  # regular
        DriverVehicleAssignment.objects.create(
            driver=self.dave, date=TARGET - timedelta(days=1), vehicle=self.u004)  # temp holder
        self._history(self.dave, self.u007, 6, end=TARGET - timedelta(days=1))
        out = suggest_day_setup(TARGET)
        rob_row = next(r for r in out["rows"] if r["driver_id"] == self.rob.id)
        dave_row = next(r for r in out["rows"] if r["driver_id"] == self.dave.id)
        self.assertEqual(rob_row["vehicle_id"], self.u004.id)
        self.assertNotEqual(dave_row["vehicle_id"], self.u004.id)
        self.assertTrue(any("goes back to" in s for s in out["swaps"]))

    def test_fluid_driver_keeps_yesterday_car(self):
        # No regular claims it: a FLUID driver (no strong usual car) keeps the unit he drove
        # most recently. A strong usual car still wins over a one-day fill-in — that is the
        # returning-regular rule, tested above.
        # filler is OFF today: pads unit usage past the rarely-used bar without competing.
        filler = _mk_driver("filler")
        DriverWeeklySchedule.objects.create(
            driver=filler, day_of_week=TARGET.weekday(), is_available=False,
            shift_type="full_day", start_hour=0, end_hour=23)
        self._history(filler, self.u013, 5, end=TARGET - timedelta(days=1))
        self._history(filler, self.u007, 5, end=TARGET - timedelta(days=6))
        # dave: mildly more #007 than #013 overall, but #013 was his last shift.
        self._history(self.dave, self.u007, 2, end=TARGET - timedelta(days=1))
        DriverVehicleAssignment.objects.create(
            driver=self.dave, date=TARGET - timedelta(days=1), vehicle=self.u013)
        out = suggest_day_setup(TARGET)
        row = next(r for r in out["rows"] if r["driver_id"] == self.dave.id)
        self.assertEqual(row["vehicle_id"], self.u013.id)
        self.assertIn("same car", row["reason"])

    def test_unchecked_available_driver_gets_prefilled_unit(self):
        # Founder: ticking an "Also available" driver should be one click — his dropdown
        # comes pre-set to the best free unit, without reserving it.
        DriverWeeklySchedule.objects.create(  # make TARGET weekday rare for dave
            driver=self.dave, day_of_week=TARGET.weekday(), is_available=True,
            shift_type="full_day", start_hour=4, end_hour=22)
        # rob works the most recent operating day, so dave has NO active streak
        DriverVehicleAssignment.objects.create(driver=self.rob, date=TARGET - timedelta(days=1),
                                               vehicle=self.u004)
        # history: dave worked 18 OLDER non-target weekdays (~3.5 weeks, so >=3 target-weekday
        # operating samples fall inside his window) -> 0/N rate, no streak -> unchecked
        added = 0
        delta = 7
        while added < 18:
            day = TARGET - timedelta(days=delta)
            delta += 1
            if day.weekday() == TARGET.weekday():
                continue
            DriverVehicleAssignment.objects.create(driver=self.dave, date=day, vehicle=self.u007)
            added += 1
        # seed target-weekday operating history WITHOUT dave (other driver works them)
        wk = 0
        delta = 1
        while wk < 4:
            day = TARGET - timedelta(days=delta)
            delta += 1
            if day.weekday() != TARGET.weekday():
                continue
            DriverVehicleAssignment.objects.get_or_create(
                driver=self.rob, date=day, defaults={"vehicle": self.u004})
            wk += 1
        out = suggest_day_setup(TARGET)
        row = next(r for r in out["rows"] if r["driver_id"] == self.dave.id)
        self.assertEqual(row["group"], "available")
        self.assertFalse(row["checked"])
        self.assertIsNotNone(row["vehicle_id"])          # prefilled...
        free_ids = {u["id"] for u in out["free_units"]}
        self.assertIn(row["vehicle_id"], free_ids)       # ...but NOT reserved

    def test_share_proposed_when_more_drivers_than_cars(self):
        # 3 checked drivers, 2 drivable cars (newguy/dave aren't Sprinter-certified, rob is):
        # the car-less driver is paired onto a colleague's unit as the PM shift with
        # partitioned planned windows — nobody is left unstaffed.
        self._history(self.rob, self.u004, 10)
        self._history(self.dave, self.u007, 10)
        self._history(self.newguy, self.u013, 3)   # u013 stays "rarely used"? no: 3 < 5 days
        # make u013 normally-used so the only shortage is bodies vs cars
        for i in range(11, 18):
            DriverVehicleAssignment.objects.create(
                driver=self.newguy, date=TARGET - timedelta(days=i), vehicle=self.u013)
        extra_u = User.objects.create_user("extra", password="x")
        extra = Driver.objects.create(profile=extra_u, driver_type="inhouse", is_active=True)
        out = suggest_day_setup(TARGET)
        shared = [r for r in out["rows"] if r.get("share")]
        self.assertEqual(len(shared), 2)   # one AM partner + one PM taker
        roles = {r["share"]["role"] for r in shared}
        self.assertEqual(roles, {"AM", "PM"})
        am = next(r for r in shared if r["share"]["role"] == "AM")
        pm = next(r for r in shared if r["share"]["role"] == "PM")
        self.assertEqual(am["vehicle_id"], pm["vehicle_id"])
        # AM End is a last-PICKUP bound one hour before the handoff, so his final job
        # clears around the handoff instead of after it; PM starts at the handoff hour.
        self.assertEqual(am["planned_end_hour"], pm["planned_start_hour"] - 1)
        self.assertTrue(any("SHARED CAR" in s for s in out["swaps"]))

    def test_inactive_holder_row_is_stale_not_locked(self):
        # neuma/shipo case: a DEACTIVATED driver still holds a unit row for the date.
        # The row must not render as crew, must not lock the unit, and must warn.
        ghost_u = User.objects.create_user("ghost", password="x")
        ghost = Driver.objects.create(profile=ghost_u, driver_type="inhouse", is_active=False)
        DriverVehicleAssignment.objects.create(driver=ghost, date=TARGET, vehicle=self.u007)
        out = suggest_day_setup(TARGET)
        self.assertNotIn(ghost.id, [r["driver_id"] for r in out["rows"]])
        # the unit is offered as free (either suggested to someone or in free_units)
        proposed_units = {r["vehicle_id"] for r in out["rows"]}
        free_ids = {u["id"] for u in out["free_units"]}
        self.assertIn(self.u007.id, proposed_units | free_ids)
        self.assertTrue(any("inactive" in w for w in out["warnings"]))

    def test_rarely_used_unit_is_last_resort_not_banned(self):
        # u013 has no history -> "rarely used". Founder rule: every car is working capacity,
        # so it CAN be suggested — but only after every normally-used unit is taken.
        self._history(self.rob, self.u004, 10)
        self._history(self.dave, self.u007, 10)
        out = suggest_day_setup(TARGET)
        # rob and dave get their usual units, NOT the rarely-used towncar...
        for did, expected in ((self.rob.id, self.u004.id), (self.dave.id, self.u007.id)):
            row = next(r for r in out["rows"] if r["driver_id"] == did)
            self.assertEqual(row["vehicle_id"], expected)
        # ...while the third driver (no other unit free) falls back to it rather than nothing.
        newguy_row = next(r for r in out["rows"] if r["driver_id"] == self.newguy.id)
        self.assertEqual(newguy_row["vehicle_id"], self.u013.id)


class DaySetupApplyTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.vt_suv = Vehicle.objects.create(vehicle_type="suv", capacity=6, luggage_capacity=6)
        cls.vt_van14 = Vehicle.objects.create(vehicle_type="Van(14 Pax)", capacity=14,
                                              luggage_capacity=14, requires_certification=True)
        cls.u1 = FleetVehicle.objects.create(vehicle_number="001", vehicle_type=cls.vt_suv,
                                             year=2023, make="Chevy", model="Suburban")
        cls.u2 = FleetVehicle.objects.create(vehicle_number="002", vehicle_type=cls.vt_suv,
                                             year=2023, make="Chevy", model="Suburban")
        cls.u3 = FleetVehicle.objects.create(vehicle_number="003", vehicle_type=cls.vt_van14,
                                             year=2022, make="Mercedes", model="Sprinter")
        cls.a = _mk_driver("alpha")
        cls.b = _mk_driver("bravo")
        cls.staff = User.objects.create_user("boss", password="x", is_staff=True)

    def _post(self, pairs, snapshot=None):
        self.client.force_login(self.staff)
        return self.client.post(
            reverse("apply_day_setup"),
            data=json.dumps({"date": TARGET.isoformat(), "pairs": pairs,
                             "snapshot": snapshot or {}}),
            content_type="application/json")

    def test_apply_creates_rows_and_is_idempotent(self):
        pairs = [{"driver_id": self.a.id, "vehicle_id": self.u1.id},
                 {"driver_id": self.b.id, "vehicle_id": self.u2.id}]
        r = self._post(pairs)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(DriverVehicleAssignment.objects.filter(date=TARGET).count(), 2)
        # idempotent re-apply (snapshot now reflects the rows)
        snap = {str(self.a.id): self.u1.id, str(self.b.id): self.u2.id}
        r2 = self._post(pairs, snapshot=snap)
        self.assertEqual(r2.status_code, 200)
        self.assertEqual(DriverVehicleAssignment.objects.filter(date=TARGET).count(), 2)

    def test_duplicate_vehicle_rejected(self):
        r = self._post([{"driver_id": self.a.id, "vehicle_id": self.u1.id},
                        {"driver_id": self.b.id, "vehicle_id": self.u1.id}])
        self.assertEqual(r.status_code, 400)
        self.assertEqual(DriverVehicleAssignment.objects.count(), 0)

    def test_cert_block(self):
        r = self._post([{"driver_id": self.a.id, "vehicle_id": self.u3.id}])
        self.assertEqual(r.status_code, 400)
        self.assertIn("isn't cleared", r.json()["error"])

    def test_unit_held_by_outsider_named(self):
        outsider = _mk_driver("charlie")
        DriverVehicleAssignment.objects.create(driver=outsider, date=TARGET, vehicle=self.u1)
        r = self._post([{"driver_id": self.a.id, "vehicle_id": self.u1.id}],
                       snapshot={str(self.a.id): None})
        self.assertEqual(r.status_code, 400)
        self.assertIn("already assigned", r.json()["error"])

    def test_preexisting_share_among_untouched_rows_does_not_block(self):
        # Founder's hand-built AM/PM share on rows the payload never touches (must-fix #1):
        # two other drivers share u3; applying an unrelated pair must succeed.
        c = _mk_driver("charlie", certified=self.vt_van14)
        d = _mk_driver("delta", certified=self.vt_van14)
        DriverVehicleAssignment.objects.create(driver=c, date=TARGET, vehicle=self.u3)
        DriverVehicleAssignment.objects.create(driver=d, date=TARGET, vehicle=self.u3)
        r = self._post([{"driver_id": self.a.id, "vehicle_id": self.u1.id}],
                       snapshot={str(self.a.id): None})
        self.assertEqual(r.status_code, 200)

    def test_drift_409(self):
        # Row appeared between preview (snapshot: none) and apply -> 409, nothing written.
        DriverVehicleAssignment.objects.create(driver=self.a, date=TARGET, vehicle=self.u2)
        r = self._post([{"driver_id": self.a.id, "vehicle_id": self.u1.id}],
                       snapshot={str(self.a.id): None})
        self.assertEqual(r.status_code, 409)
        self.assertEqual(
            DriverVehicleAssignment.objects.get(driver=self.a, date=TARGET).vehicle_id,
            self.u2.id)

    def test_share_pair_applies_with_planned_windows(self):
        pairs = [{"driver_id": self.a.id, "vehicle_id": self.u1.id, "allow_share": True,
                  "planned_start_hour": 4, "planned_end_hour": 15},
                 {"driver_id": self.b.id, "vehicle_id": self.u1.id, "allow_share": True,
                  "planned_start_hour": 15, "planned_end_hour": 23}]
        r = self._post(pairs)
        self.assertEqual(r.status_code, 200)
        rows = DriverVehicleAssignment.objects.filter(date=TARGET, vehicle=self.u1)
        self.assertEqual(rows.count(), 2)
        a_row = rows.get(driver=self.a)
        self.assertEqual((a_row.planned_start_hour, a_row.planned_end_hour), (4, 15))
        b_row = rows.get(driver=self.b)
        self.assertEqual((b_row.planned_start_hour, b_row.planned_end_hour), (15, 23))

    def test_accidental_duplicate_still_rejected(self):
        # Same unit twice WITHOUT the share flag on every pair = a mistake, not a share.
        pairs = [{"driver_id": self.a.id, "vehicle_id": self.u1.id, "allow_share": True},
                 {"driver_id": self.b.id, "vehicle_id": self.u1.id}]
        self.assertEqual(self._post(pairs).status_code, 400)

    def test_inactive_holder_does_not_block_apply(self):
        # A stale row held by a deactivated driver must not 400 an unrelated assignment
        # of the same unit to a real driver.
        ghost_u = User.objects.create_user("ghost2", password="x")
        ghost = Driver.objects.create(profile=ghost_u, driver_type="inhouse", is_active=False)
        DriverVehicleAssignment.objects.create(driver=ghost, date=TARGET, vehicle=self.u1)
        r = self._post([{"driver_id": self.a.id, "vehicle_id": self.u1.id}],
                       snapshot={str(self.a.id): None})
        self.assertEqual(r.status_code, 200)

    def test_non_staff_403(self):
        u = User.objects.create_user("pleb", password="x", is_staff=False)
        self.client.force_login(u)
        r = self.client.post(reverse("apply_day_setup"), data="{}",
                             content_type="application/json")
        self.assertEqual(r.status_code, 403)
