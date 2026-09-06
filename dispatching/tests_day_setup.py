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

    def _more_drivers_than_cars(self):
        """4 checked drivers, 3 units (only rob can drive the Sprinter) -> 1 unmatched."""
        self._history(self.rob, self.u004, 10)
        self._history(self.dave, self.u007, 10)
        self._history(self.newguy, self.u013, 3)
        # make u013 normally-used so the only shortage is bodies vs cars
        for i in range(11, 18):
            DriverVehicleAssignment.objects.create(
                driver=self.newguy, date=TARGET - timedelta(days=i), vehicle=self.u013)
        extra_u = User.objects.create_user("extra", password="x")
        return Driver.objects.create(profile=extra_u, driver_type="inhouse", is_active=True)

    def test_share_proposed_when_more_drivers_than_cars(self):
        # LEGACY auto-share branch (solo_first=False — the one-toggle-away path): the
        # car-less driver is paired onto a colleague's unit as the PM shift with
        # partitioned planned windows — nobody is left unstaffed.
        self._more_drivers_than_cars()
        out = suggest_day_setup(TARGET, solo_first=False)
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

    def test_solo_first_extra_left_unchecked(self):
        # SOLO-FIRST default: same shortage, but no share is proposed — the extra stays
        # UNCHECKED with the "add via Advisor" hint and an aggregated callout explains it.
        self._more_drivers_than_cars()
        out = suggest_day_setup(TARGET)   # DAY_SETUP_SOLO_FIRST=True default
        self.assertFalse([r for r in out["rows"] if r.get("share")])
        unchecked = [r for r in out["rows"]
                     if r["group"] == "available" and "add via Advisor" in r["hint"]]
        self.assertEqual(len(unchecked), 1)
        row = unchecked[0]
        self.assertFalse(row["checked"])
        self.assertIsNone(row["vehicle_id"])
        self.assertNotIn("planned_start_hour", row)
        self.assertTrue(any("MORE DRIVERS THAN CARS" in s for s in out["swaps"]))
        self.assertFalse(any("SHARED CAR" in s for s in out["swaps"]))
        self.assertFalse(any("No free unit" in w for w in out["warnings"]))

    def test_solo_first_noop_when_cars_suffice(self):
        # With enough cars for every checked driver the flag changes NOTHING.
        self._history(self.rob, self.u004, 10)
        self._history(self.dave, self.u007, 10)
        self.assertEqual(suggest_day_setup(TARGET),
                         suggest_day_setup(TARGET, solo_first=False))

    def test_suggest_view_solo_first_passthrough(self):
        # The endpoint's solo_first key A/Bs the behavior per request (harness + console).
        self._more_drivers_than_cars()
        staff = User.objects.create_user("boss_ds", password="x", is_staff=True)
        self.client.force_login(staff)

        def post(payload):
            r = self.client.post(reverse("suggest_day_setup"), data=json.dumps(payload),
                                 content_type="application/json")
            self.assertEqual(r.status_code, 200)
            return r.json()

        default = post({"date": TARGET.isoformat()})
        legacy = post({"date": TARGET.isoformat(), "solo_first": False})
        self.assertFalse([r for r in default["rows"] if r.get("share")])
        self.assertTrue([r for r in legacy["rows"] if r.get("share")])

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

    def test_orphaned_share_row_becomes_solo_day(self):
        # Founder unchecks the PM half of a proposed share in the modal: the remaining
        # AM row still arrives with allow_share + the truncated window. Declining a
        # split must mean "one driver keeps the car ALL day" — the orphaned share flag
        # and partitioned window are stripped, never persisted.
        pairs = [{"driver_id": self.a.id, "vehicle_id": self.u1.id, "allow_share": True,
                  "planned_start_hour": 4, "planned_end_hour": 14}]
        r = self._post(pairs)
        self.assertEqual(r.status_code, 200)
        row = DriverVehicleAssignment.objects.get(date=TARGET, driver=self.a)
        self.assertEqual(row.vehicle_id, self.u1.id)
        self.assertIsNone(row.planned_start_hour)
        self.assertIsNone(row.planned_end_hour)

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

    # ── Build 2a: the ≤2-drivers hard rule + planned windows on BOTH rows ──

    def test_three_drivers_on_one_unit_rejected(self):
        c = _mk_driver("charlie")
        pairs = [{"driver_id": d.id, "vehicle_id": self.u1.id, "allow_share": True}
                 for d in (self.a, self.b, c)]
        r = self._post(pairs)
        self.assertEqual(r.status_code, 400)
        self.assertIn("at most two", r.json()["error"])
        self.assertEqual(DriverVehicleAssignment.objects.count(), 0)

    def test_share_onto_already_shared_unit_rejected(self):
        # Two real holders outside the payload + one more allow_share pair = 3.
        c = _mk_driver("charlie")
        d = _mk_driver("delta")
        DriverVehicleAssignment.objects.create(driver=c, date=TARGET, vehicle=self.u1)
        DriverVehicleAssignment.objects.create(driver=d, date=TARGET, vehicle=self.u1)
        r = self._post([{"driver_id": self.a.id, "vehicle_id": self.u1.id,
                         "allow_share": True, "planned_start_hour": 16,
                         "planned_end_hour": 23}])
        self.assertEqual(r.status_code, 400)
        self.assertIn("at most two", r.json()["error"])

    def test_single_pair_share_fills_both_windows(self):
        # The Second-Shift Advisor / standby-proposal shape: holder keeps his row
        # (NOT in the payload), the new PM pair arrives alone with allow_share.
        # Build 2a: the plan is written on BOTH rows.
        holder = _mk_driver("charlie")
        h_row = DriverVehicleAssignment.objects.create(
            driver=holder, date=TARGET, vehicle=self.u1)
        r = self._post([{"driver_id": self.a.id, "vehicle_id": self.u1.id,
                         "allow_share": True, "planned_start_hour": 16,
                         "planned_end_hour": 23}])
        self.assertEqual(r.status_code, 200)
        a_row = DriverVehicleAssignment.objects.get(date=TARGET, driver=self.a)
        self.assertEqual((a_row.planned_start_hour, a_row.planned_end_hour), (16, 23))
        h_row.refresh_from_db()
        self.assertEqual((h_row.planned_start_hour, h_row.planned_end_hour), (4, 15))

    def test_single_pair_share_without_hours_uses_settings_cut(self):
        from dispatching.models import SchedulerSettings
        cfg = SchedulerSettings.get_settings()
        cfg.share_split_hour = 17
        cfg.save()
        SchedulerSettings.clear_cache()
        try:
            holder = _mk_driver("charlie")
            h_row = DriverVehicleAssignment.objects.create(
                driver=holder, date=TARGET, vehicle=self.u1)
            r = self._post([{"driver_id": self.a.id, "vehicle_id": self.u1.id,
                             "allow_share": True}])
            self.assertEqual(r.status_code, 200)
            a_row = DriverVehicleAssignment.objects.get(date=TARGET, driver=self.a)
            self.assertEqual((a_row.planned_start_hour, a_row.planned_end_hour), (17, 23))
            h_row.refresh_from_db()
            self.assertEqual((h_row.planned_start_hour, h_row.planned_end_hour), (4, 16))
        finally:
            SchedulerSettings.get_settings().reset_to_defaults()

    def test_single_pair_share_never_clobbers_partner_window(self):
        # The holder already carries a hand-set window: Apply fills only NULLs.
        holder = _mk_driver("charlie")
        h_row = DriverVehicleAssignment.objects.create(
            driver=holder, date=TARGET, vehicle=self.u1,
            planned_start_hour=5, planned_end_hour=14)
        r = self._post([{"driver_id": self.a.id, "vehicle_id": self.u1.id,
                         "allow_share": True, "planned_start_hour": 16,
                         "planned_end_hour": 23}])
        self.assertEqual(r.status_code, 200)
        h_row.refresh_from_db()
        self.assertEqual((h_row.planned_start_hour, h_row.planned_end_hour), (5, 14))

    def test_two_pair_share_without_hours_gets_default_partition(self):
        pairs = [{"driver_id": self.a.id, "vehicle_id": self.u1.id, "allow_share": True},
                 {"driver_id": self.b.id, "vehicle_id": self.u1.id, "allow_share": True}]
        r = self._post(pairs)
        self.assertEqual(r.status_code, 200)
        rows = {x.driver_id: x for x in
                DriverVehicleAssignment.objects.filter(date=TARGET, vehicle=self.u1)}
        wins = sorted((x.planned_start_hour, x.planned_end_hour)
                      for x in rows.values())
        self.assertEqual(wins, [(4, 15), (16, 23)])   # default cut = 16


class DaySetupSplitShiftExtrasTests(TestCase):
    """Build 2b/2c/2d payload keys on a built day: a rostered early driver, one
    farmed evening leg, one standby body — the panel must propose the mint,
    stamp the span readout, and leave every classic key untouched."""

    @classmethod
    def setUpTestData(cls):
        from decimal import Decimal
        from rates.models import Location, Rate, Route
        from reservations.models import Customer, Reservation

        cls.vt = Vehicle.objects.create(vehicle_type="suv", capacity=6,
                                        luggage_capacity=4)
        cls.u1 = FleetVehicle.objects.create(vehicle_number="007", vehicle_type=cls.vt,
                                             year=2023, make="Chevy", model="Suburban")
        cls.worker = _mk_driver("worker")
        cls.standby = _mk_driver("standy")
        cls.aff = Driver.objects.create(
            profile=User.objects.create_user("affco"), driver_type="affiliate",
            is_active=True)
        origin = Location.objects.create(name="MCO")
        dest = Location.objects.create(name="Disney")
        route = Route.objects.create(origin=origin, destination=dest,
                                     inhouse_base_pay=Decimal("50.00"))
        rate = Rate.objects.create(route=route, vehicle=cls.vt,
                                   oneway_price=Decimal("100.00"),
                                   round_trip_price=Decimal("180.00"))
        customer = Customer.objects.create(first_name="Pat", last_name="Guest",
                                           email="pat@example.com",
                                           phone_number="5550001111")
        cls.res = Reservation.objects.create(
            trip_type="one-way", customer=customer, vehicle=cls.vt, rate=rate,
            base_price=Decimal("100.00"), total_price=Decimal("100.00"))
        cls.route = route
        DriverVehicleAssignment.objects.create(driver=cls.worker, date=TARGET,
                                               vehicle=cls.u1)

    def _leg(self, hh, mm=0, driver=None,
             pickup="Disney's Animal Kingdom Lodge",
             dropoff="Orlando International Airport (MCO)"):
        from datetime import time
        from reservations.models import Leg
        return Leg.objects.create(
            reservation=self.res, pickup_date=TARGET, pickup_time=time(hh, mm),
            pickup_location=pickup, dropoff_location=dropoff, driver=driver,
            route=self.route, status="confirmed")

    def test_mint_proposed_for_farmed_evening_leg(self):
        self._leg(5, 0, driver=self.worker)
        self._leg(10, 0, driver=self.worker)
        farmed = self._leg(18, 0, driver=self.aff,
                           pickup="Orlando International Airport (MCO)",
                           dropoff="Disney's Animal Kingdom Lodge")
        out = suggest_day_setup(TARGET)
        self.assertIn(self.standby.id,
                      [p["id"] for p in out["standby_pool"]])
        self.assertEqual(len(out["mint_proposals"]), 1)
        mp = out["mint_proposals"][0]
        self.assertEqual(mp["driver_id"], self.standby.id)
        self.assertEqual(mp["vehicle_id"], self.u1.id)
        self.assertEqual(mp["side"], "late")
        self.assertEqual([l["id"] for l in mp["legs"]], [farmed.id])
        self.assertTrue(mp["thin"])                      # 1 job < soft min 2
        self.assertGreater(mp["est_saving"], 0)
        self.assertIn(mp["handoff_band"], ("green", "amber"))  # red never proposed
        self.assertEqual(mp["planned_start_hour"], 18)
        self.assertEqual(mp["planned_end_hour"], 23)
        self.assertEqual(mp["partner_driver_id"], self.worker.id)
        # span readout on the worker's row (2d)
        row = next(r for r in out["rows"] if r["driver_id"] == self.worker.id)
        self.assertIn("span_hours", row)
        self.assertEqual(row["span_state"], "")
        self.assertEqual(out["span_exceptions"], [])
        self.assertEqual(out["shared_units"], [])

    def test_worked_driver_not_in_pool_and_cold_day_proposes_nothing(self):
        out = suggest_day_setup(TARGET)   # no legs at all on the date
        self.assertEqual(out["mint_proposals"], [])
        pool_ids = [p["id"] for p in out["standby_pool"]]
        self.assertNotIn(self.worker.id, pool_ids)   # has a DVA row
        self.assertIn(self.standby.id, pool_ids)

    def test_shared_unit_banded(self):
        second = _mk_driver("second")
        DriverVehicleAssignment.objects.create(driver=second, date=TARGET,
                                               vehicle=self.u1)
        self._leg(5, 0, driver=self.worker)
        self._leg(9, 0, driver=self.worker)
        self._leg(19, 0, driver=second,
                  pickup="Orlando International Airport (MCO)",
                  dropoff="Disney's Animal Kingdom Lodge")
        out = suggest_day_setup(TARGET)
        self.assertEqual(len(out["shared_units"]), 1)
        su = out["shared_units"][0]
        self.assertEqual(su["vehicle_id"], self.u1.id)
        self.assertEqual(su["handoff_band"], "green")
        self.assertTrue(su["handoff_ready_at"])
        self.assertIn("chain", su["handoff_reason"])


class SchedulerSettingsFloatTests(TestCase):
    def test_float_field_round_trips(self):
        staff = User.objects.create_user("boss2", password="x", is_staff=True)
        self.client.force_login(staff)
        r = self.client.post(reverse("update_scheduler_settings"),
                             data=json.dumps({"span_exception_max_hours": 14.5}),
                             content_type="application/json")
        self.assertEqual(r.status_code, 200)
        from dispatching.models import SchedulerSettings
        SchedulerSettings.clear_cache()
        self.assertEqual(SchedulerSettings.get_settings().span_exception_max_hours, 14.5)
        SchedulerSettings.get_settings().reset_to_defaults()

    def test_int_fields_still_int_only(self):
        staff = User.objects.create_user("boss3", password="x", is_staff=True)
        self.client.force_login(staff)
        r = self.client.post(reverse("update_scheduler_settings"),
                             data=json.dumps({"share_split_hour": "nope"}),
                             content_type="application/json")
        self.assertEqual(r.status_code, 400)


def _fake_end(leg, target_date):
    """Deterministic 60-min jobs for peak-histogram tests."""
    from datetime import datetime, timedelta as _td
    return datetime.combine(target_date, leg.pickup_time) + _td(minutes=60)


class DaySetupPeakSizingTests(TestCase):
    """Peak-concurrency roster sizing — founder rule: size by the in-flight histogram
    per vehicle tier, never by daily totals or naive legs-per-driver."""

    @classmethod
    def setUpTestData(cls):
        from reservations.models import Reservation, Customer
        cls.vt_suv = Vehicle.objects.create(vehicle_type="suv", capacity=6, luggage_capacity=6)
        cls.vt_van14 = Vehicle.objects.create(vehicle_type="Van(14 Pax)", capacity=14,
                                              luggage_capacity=14, requires_certification=True)
        cls.units = [FleetVehicle.objects.create(vehicle_number=f"0{i}", vehicle_type=cls.vt_suv,
                                                 year=2023, make="Chevy", model="Suburban")
                     for i in range(1, 7)]
        cls.u_van14 = FleetVehicle.objects.create(vehicle_number="09", vehicle_type=cls.vt_van14,
                                                  year=2022, make="Mercedes", model="Sprinter")
        # burner rows so no fixture driver lands on the hardcoded excluded id 6
        for i in range(6):
            u = User.objects.create_user(f"pkburn{i}", password="x")
            Driver.objects.create(profile=u, driver_type="inhouse", is_active=False)
        cls.drivers = [_mk_driver(f"pk{i}") for i in range(1, 7)]
        from rates.models import Location, Route, Rate
        cust = Customer.objects.create(first_name="Test", email="t@example.com",
                                       phone_number="555-0000")
        loc_a = Location.objects.create(name="A")
        loc_b = Location.objects.create(name="B")
        route = Route.objects.create(origin=loc_a, destination=loc_b)
        rate_suv = Rate.objects.create(vehicle=cls.vt_suv, route=route,
                                       oneway_price=100, round_trip_price=180)
        rate_v14 = Rate.objects.create(vehicle=cls.vt_van14, route=route,
                                       oneway_price=200, round_trip_price=360)
        cls.res_suv = Reservation.objects.create(customer=cust, vehicle=cls.vt_suv,
                                                 rate=rate_suv)
        cls.res_v14 = Reservation.objects.create(customer=cust, vehicle=cls.vt_van14,
                                                 rate=rate_v14)
        cls.res_untyped = Reservation.objects.create(customer=cust, rate=rate_suv)

    def _leg(self, res, h, m=0):
        from reservations.models import Leg
        from datetime import time
        return Leg.objects.create(reservation=res, pickup_date=TARGET,
                                  pickup_time=time(h, m), pickup_location="A",
                                  dropoff_location="B")

    def test_peak_concurrency_histogram(self):
        from unittest.mock import patch
        from dispatching.day_setup import peak_concurrency
        # 9:00, 9:30, 11:00 suv (60-min jobs) + 9:30 van14 + 9:30 untyped:
        # in flight at 9:30 = 9:00suv + 9:30suv + 9:30van14 + 9:30untyped = 4 overall
        # (11:00 is isolated; untyped counts in overall only).
        for h, m in ((9, 0), (9, 30), (11, 0)):
            self._leg(self.res_suv, h, m)
        self._leg(self.res_v14, 9, 30)
        self._leg(self.res_untyped, 9, 30)
        with patch("dispatching.scheduler.estimate_job_end_time", _fake_end):
            pk = peak_concurrency(TARGET)
        self.assertEqual(pk["overall"][0], 4)
        self.assertEqual(pk["overall"][1].strftime("%H:%M"), "09:30")
        self.assertEqual(pk["per_tier"]["suv"][0], 2)            # exact-tier
        self.assertEqual(pk["per_tier"]["Van(14 Pax)"][0], 1)
        self.assertEqual(pk["cumulative"]["suv"][0], 3)          # suv + the van14 above
        self.assertEqual(pk["total_legs"], 5)

    def test_peak_sizing_caps_checked(self):
        from unittest.mock import patch
        # peak 2 (9:00 + 9:30 overlap) + buffer 1 -> 3 checked of 6 available.
        self._leg(self.res_suv, 9, 0)
        self._leg(self.res_suv, 9, 30)
        with patch("dispatching.scheduler.estimate_job_end_time", _fake_end):
            out = suggest_day_setup(TARGET)
        checked = [r for r in out["rows"] if r["checked"]]
        capped = [r for r in out["rows"] if "peak needs only" in (r["hint"] or "")]
        self.assertEqual(len(checked), 3)
        self.assertEqual(len(capped), 3)
        self.assertTrue(all(not r["checked"] and r["group"] == "available" for r in capped))
        # The old "PEAK DEMAND: ..." banner quoted a driver count taken before the
        # solo-first pass unchecked the car-less, so it could contradict the list it sat
        # above. It is replaced by structured `capacity` + a settled headcount line.
        self.assertFalse(any(s.startswith("PEAK DEMAND:") for s in out["swaps"]))
        left = [s for s in out["swaps"] if s.startswith("Left available:")]
        self.assertEqual(len(left), 1)
        self.assertIn(f"{len(checked)} drivers covers the busiest moment", left[0])
        self.assertIsNotNone(out["peak"])
        self.assertEqual(out["peak"]["overall"]["n"], 2)

    def test_peak_cert_guard(self):
        from unittest.mock import patch
        # A van14 leg is in flight: the ONLY certified driver must survive the cap even
        # though everyone shares the same rank.
        self._leg(self.res_v14, 9, 0)
        self._leg(self.res_suv, 9, 30)
        certified = self.drivers[-1]              # highest id = first drop candidate
        certified.certified_vehicle_types.add(self.vt_van14)
        with patch("dispatching.scheduler.estimate_job_end_time", _fake_end):
            out = suggest_day_setup(TARGET)
        row = next(r for r in out["rows"] if r["driver_id"] == certified.id)
        self.assertTrue(row["checked"])

    def test_peak_sizing_flag_off(self):
        from unittest.mock import patch
        self._leg(self.res_suv, 9, 0)
        self._leg(self.res_suv, 9, 30)
        with patch("dispatching.scheduler.estimate_job_end_time", _fake_end):
            out = suggest_day_setup(TARGET, peak_sizing=False)
        self.assertEqual(len([r for r in out["rows"] if r["checked"]]), 6)
        self.assertFalse(any(s.startswith("PEAK DEMAND:") for s in out["swaps"]))
        self.assertIsNone(out["peak"])

    def test_no_legs_skips_peak_sizing(self):
        out = suggest_day_setup(TARGET)
        self.assertEqual(len([r for r in out["rows"] if r["checked"]]), 6)
        self.assertFalse(any(s.startswith("PEAK DEMAND:") for s in out["swaps"]))

    def test_p2_reservation_uses_cumulative_peak(self):
        from unittest.mock import patch
        # Overlapping suv + van14 legs: cumulative suv peak = 2 -> two units of
        # tier >= suv get reserved/proposed (exact-tier counting would reserve 1).
        self._leg(self.res_suv, 9, 0)
        self._leg(self.res_v14, 9, 30)
        for d in self.drivers:
            d.certified_vehicle_types.add(self.vt_van14)
        with patch("dispatching.scheduler.estimate_job_end_time", _fake_end):
            out = suggest_day_setup(TARGET)
        proposed_units = {r["vehicle_id"] for r in out["rows"] if r["vehicle_id"]}
        self.assertGreaterEqual(len(proposed_units), 2)
        self.assertIn(self.u_van14.id, proposed_units)

    def test_tier_need_capped_at_fleet_and_no_impossible_warning(self):
        from unittest.mock import patch
        # 9 overlapping suv legs against a 7-car fleet. Uncapped, P2 would "need" 9
        # suv-capable units and warn about a shortfall no dispatcher can act on
        # (the real 07-11 case: 19 towncar-capable wanted, 13 cars owned).
        for m in range(0, 45, 5):
            self._leg(self.res_suv, 9, m)
        with patch("dispatching.scheduler.estimate_job_end_time", _fake_end):
            out = suggest_day_setup(TARGET)
        self.assertFalse([w for w in out["warnings"] if "unit(s) of that size" in w],
                         "impossible per-size shortfall must not be reported as a finding")
        # ...and the day is correctly read as one where every car is needed.
        self.assertEqual(out["capacity"]["can_park"], [])

    def test_parkable_units_parks_least_capable_first(self):
        from dispatching.day_setup import parkable_units
        from datetime import datetime
        at = datetime.combine(TARGET, datetime.min.time())
        # Fleet: 6 suv + 1 van14. One van14 trip and two suv trips at the peak.
        # Nested compatibility means the van14 can serve suv work, so 3 cars must run
        # and the 4 smallest (suv) can stay in.
        cum = {"Van(14 Pax)": (1, at), "suv": (3, at)}
        parked, staffed = parkable_units(self.units + [self.u_van14], cum)
        self.assertEqual(len(staffed), 3)
        self.assertEqual(len(parked), 4)
        self.assertTrue(all(u.vehicle_type == self.vt_suv for u in parked),
                        "never park the Sprinter while smaller cars are still out")
        self.assertIn(self.u_van14, staffed)

    def test_untyped_legs_still_need_a_body(self):
        from unittest.mock import patch
        # A reservation with no vehicle set appears in the OVERALL peak but in no tier.
        # Without the overall floor every per-size test passes vacuously and the whole
        # fleet reads as parkable — "quiet day, park all 7 cars" on a day with work.
        self._leg(self.res_untyped, 9, 0)
        self._leg(self.res_untyped, 9, 30)
        with patch("dispatching.scheduler.estimate_job_end_time", _fake_end):
            out = suggest_day_setup(TARGET)
        self.assertEqual(out["capacity"]["must_run"], 2)
        self.assertEqual(len(out["capacity"]["can_park"]), 5)

    def test_parkable_units_busy_day_parks_nothing(self):
        from dispatching.day_setup import parkable_units
        from datetime import datetime
        at = datetime.combine(TARGET, datetime.min.time())
        cum = {"suv": (7, at)}
        parked, staffed = parkable_units(self.units + [self.u_van14], cum)
        self.assertEqual(parked, [])
        self.assertEqual(len(staffed), 7)

    def test_quiet_day_names_the_cars_that_can_stay_in(self):
        from unittest.mock import patch
        self._leg(self.res_suv, 9, 0)          # one trip, seven cars
        with patch("dispatching.scheduler.estimate_job_end_time", _fake_end):
            out = suggest_day_setup(TARGET)
        self.assertEqual(len(out["capacity"]["can_park"]), 6)
        self.assertEqual(out["capacity"]["must_run"], 1)
        self.assertTrue(any(s.startswith("Quiet day —") for s in out["swaps"]))

    def test_concurrency_series_matches_the_peak(self):
        from unittest.mock import patch
        from dispatching.day_setup import concurrency_series, peak_concurrency
        from reservations.models import Leg
        self._leg(self.res_suv, 9, 0)
        self._leg(self.res_suv, 9, 30)
        self._leg(self.res_v14, 9, 30)
        with patch("dispatching.scheduler.estimate_job_end_time", _fake_end):
            legs = list(Leg.objects.filter(pickup_date=TARGET))
            series = concurrency_series(TARGET, legs)
            pk = peak_concurrency(TARGET, legs=legs)
        # the chart and the headline number can never disagree
        self.assertEqual(max(s["n"] for s in series), pk["overall"][0])
        top = next(s for s in series if s["n"] == pk["overall"][0])
        self.assertEqual(top["t"], pk["overall"][1].strftime("%H:%M"))
        self.assertEqual(top["tiers"], {"suv": 2, "Van(14 Pax)": 1})

    def test_rows_sort_by_unit_number_not_driver_name(self):
        from unittest.mock import patch
        # Founder reads the fleet in unit order, so the modal must too — and "#10" is
        # unit TEN, which belongs after #009, never between #002 and #03.
        u10 = FleetVehicle.objects.create(vehicle_number="10", vehicle_type=self.vt_suv,
                                          year=2023, make="Chevy", model="Suburban")
        for m in range(0, 30, 5):
            self._leg(self.res_suv, 9, m)
        with patch("dispatching.scheduler.estimate_job_end_time", _fake_end):
            out = suggest_day_setup(TARGET)
        seated = [r["vehicle_label"] for r in out["rows"]
                  if r["group"] == "suggested" and r["vehicle_id"]]
        nums = [lbl.split()[0] for lbl in seated]
        self.assertEqual(nums, sorted(nums, key=lambda s: int(s.lstrip("#"))),
                         "crew must run #001, #002 ... #009, #10 — numeric, not string")
        self.assertGreater(len(nums), 1)
        u10.delete()

    def test_logic_is_fleet_size_agnostic(self):
        from unittest.mock import patch
        from dispatching.day_setup import parkable_units
        from datetime import datetime
        # Nothing in the suggester may assume a fleet size. Grow 7 cars to 20 —
        # spanning one-, two- and three-digit unit numbers — and the same rules must
        # hold: numeric ordering (#009 < #10 < #20, never string order), size targets
        # capped at whatever the fleet actually is, and park-the-least-capable-first
        # scaling with the extra cars rather than saturating.
        extra = [FleetVehicle.objects.create(vehicle_number=n, vehicle_type=self.vt_suv,
                                             year=2023, make="Chevy", model="Suburban")
                 for n in ("10", "11", "12", "13", "14", "15", "16", "17", "18", "020", "7")]
        try:
            fleet = list(self.units) + [self.u_van14] + extra
            self.assertEqual(len(fleet), 18)
            at = datetime.combine(TARGET, datetime.min.time())

            # Park scales with the fleet: 3 concurrent suv trips out of 19 cars.
            parked, staffed = parkable_units(fleet, {"suv": (3, at)}, overall=3)
            self.assertEqual(len(staffed), 3)
            self.assertEqual(len(parked), 15)
            # ...and the Sprinter is the LAST thing parked, whatever the fleet size.
            self.assertIn(self.u_van14, staffed + parked[-1:])

            # A busy day on a big fleet still parks nothing.
            parked2, staffed2 = parkable_units(fleet, {"suv": (18, at)}, overall=18)
            self.assertEqual(parked2, [])
            self.assertEqual(len(staffed2), 18)

            for m in range(0, 30, 5):
                self._leg(self.res_suv, 9, m)
            with patch("dispatching.scheduler.estimate_job_end_time", _fake_end):
                out = suggest_day_setup(TARGET)
            self.assertEqual(out["capacity"]["fleet"], 18)
            # No impossible per-size target survives on an 18-car fleet either.
            self.assertFalse([w for w in out["warnings"] if "unit(s) of that size" in w])
            # Unit ordering is numeric across all three digit-widths.
            labels = [o["label"] for o in
                      next(r for r in out["rows"] if r["group"] in ("suggested", "available")
                           and r.get("unit_options"))["unit_options"]]
            nums = [int(l.split()[0].lstrip("#")) for l in labels]
            self.assertEqual(sorted(nums), sorted(set(nums)), "no duplicate units offered")
            self.assertIn(20, nums, "#020 must be offered and read as unit twenty")
        finally:
            for u in extra:
                u.delete()

    def test_a_parked_car_is_offered_to_only_one_driver(self):
        from unittest.mock import patch
        # Two drivers can both score best on the same idle car; telling both that it
        # "suits him better" advertises two fixes and delivers one.
        for m in range(0, 25, 5):
            self._leg(self.res_suv, 9, m)
        with patch("dispatching.scheduler.estimate_job_end_time", _fake_end):
            out = suggest_day_setup(TARGET)
        offers = [r["reason"] for r in out["rows"] if "suits him better" in str(r["reason"])]
        self.assertEqual(len(offers), len(set(offers)),
                         "the same idle car must not be offered to two rows")

    def test_no_covers_demand_chip_survives(self):
        from unittest.mock import patch
        self._leg(self.res_suv, 9, 0)
        self._leg(self.res_suv, 9, 30)
        with patch("dispatching.scheduler.estimate_job_end_time", _fake_end):
            out = suggest_day_setup(TARGET)
        # "covers <tier> demand (N legs)" was the engine's least reliable signal (56%)
        # dressed as its strongest, and quoted the day's total leg count on every row.
        self.assertFalse([r for r in out["rows"]
                          if str(r["reason"]).startswith("covers ")])
        seated = [r for r in out["rows"] if r["checked"] and r["vehicle_id"]]
        self.assertTrue(seated)
        self.assertTrue(all(r["reason"] for r in seated))


class DaySetupForceIncludeTests(TestCase):
    """Force-include ("Yovanny in, someone out") — P3d displacement."""

    @classmethod
    def setUpTestData(cls):
        cls.vt_suv = Vehicle.objects.create(vehicle_type="suv", capacity=6, luggage_capacity=6)
        cls.vt_van14 = Vehicle.objects.create(vehicle_type="Van(14 Pax)", capacity=14,
                                              luggage_capacity=14, requires_certification=True)
        cls.u004 = FleetVehicle.objects.create(vehicle_number="004", vehicle_type=cls.vt_van14,
                                               year=2022, make="Mercedes", model="Sprinter")
        cls.u007 = FleetVehicle.objects.create(vehicle_number="007", vehicle_type=cls.vt_suv,
                                               year=2023, make="Chevy", model="Suburban")
        cls.rob = _mk_driver("rob2", certified=cls.vt_van14)
        cls.dave = _mk_driver("dave2")
        cls.newguy = _mk_driver("newguy2")
        cls.staff = User.objects.create_user("boss4", password="x", is_staff=True)

    def _history(self, driver, unit, days):
        for i in range(1, days + 1):
            DriverVehicleAssignment.objects.create(
                driver=driver, date=TARGET - timedelta(days=i), vehicle=unit)

    def test_force_include_displaces_lowest_priority(self):
        # rob keeps the Sprinter (dedicated), dave wins u007 by history but stays UNDER
        # the dedicated-lock threshold; forcing newguy displaces dave (lowest
        # non-forced, non-dedicated) — "Yovanny in, someone out".
        self._history(self.rob, self.u004, 10)
        self._history(self.dave, self.u007, 5)
        out = suggest_day_setup(TARGET, force_include=[self.newguy.id])
        new_row = next(r for r in out["rows"] if r["driver_id"] == self.newguy.id)
        dave_row = next(r for r in out["rows"] if r["driver_id"] == self.dave.id)
        self.assertTrue(new_row["checked"])
        self.assertTrue(new_row.get("forced"))
        self.assertEqual(new_row["vehicle_id"], self.u007.id)
        self.assertFalse(dave_row["checked"])
        self.assertIn("stepped aside", dave_row["hint"])
        self.assertTrue(any(" in, " in s for s in out["swaps"]))

    def test_force_include_respects_dedicated_locks(self):
        # rob's unit is a dedicated lock and the ONLY unit newguy could take -> P3d
        # refuses with the loud warning instead of bumping a dedicated regular.
        self.u004.delete()
        self._history(self.rob, self.u007, 20)   # dedicated: share 100% over 20 days
        out = suggest_day_setup(TARGET, force_include=[self.newguy.id])
        rob_row = next(r for r in out["rows"] if r["driver_id"] == self.rob.id)
        self.assertEqual(rob_row["vehicle_id"], self.u007.id)
        self.assertTrue(any("Couldn't seat" in w for w in out["warnings"]))

    def test_force_include_off_driver_refused(self):
        DriverWeeklySchedule.objects.create(
            driver=self.newguy, day_of_week=TARGET.weekday(), is_available=False,
            shift_type="full_day", start_hour=0, end_hour=23)
        out = suggest_day_setup(TARGET, force_include=[self.newguy.id])
        row = next(r for r in out["rows"] if r["driver_id"] == self.newguy.id)
        self.assertEqual(row["group"], "off")
        self.assertFalse(row["checked"])
        self.assertTrue(any("schedule says OFF" in w and "Advisor" in w
                            for w in out["warnings"]))

    def test_force_exclude_unchecks(self):
        self._history(self.dave, self.u007, 10)
        out = suggest_day_setup(TARGET, force_exclude=[self.dave.id])
        row = next(r for r in out["rows"] if r["driver_id"] == self.dave.id)
        self.assertFalse(row["checked"])
        self.assertEqual(row["hint"], "unchecked by you")

    def test_unknown_forced_id_warns(self):
        out = suggest_day_setup(TARGET, force_include=[999999])
        self.assertTrue(any("unknown/inactive/excluded" in w for w in out["warnings"]))

    def test_suggest_view_force_passthrough(self):
        self._history(self.rob, self.u004, 10)
        self._history(self.dave, self.u007, 10)
        self.client.force_login(self.staff)
        r = self.client.post(reverse("suggest_day_setup"),
                             data=json.dumps({"date": TARGET.isoformat(),
                                              "force_include": [self.newguy.id]}),
                             content_type="application/json")
        self.assertEqual(r.status_code, 200)
        row = next(x for x in r.json()["rows"] if x["driver_id"] == self.newguy.id)
        self.assertTrue(row.get("forced"))
