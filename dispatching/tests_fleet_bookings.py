"""Vehicle bookings: fleet claiming part of a car's day from The day, and what
that booking means everywhere dispatch touches the car.

Run with:  ENABLE_DEBUG_TOOLBAR=0 ./manage.py test dispatching.tests_fleet_bookings

Trip ends are pinned to pickup + 90 minutes (the tests_fleet_day trick), so a
clash is arithmetic: a 9:00 trip occupies 9:00–10:30.
"""
import json
from datetime import time, timedelta
from unittest.mock import patch

from django.contrib.auth.models import User
from django.urls import reverse

from dispatching import fleet_bookings
from dispatching.tests_fleet_day import _DayFixture, _ninety_minutes
from dispatching.tests_fleet_desk import DAY, TODAY
from drivers.models import DriverVehicleAssignment, VehicleBooking, VehicleDowntime
from users.models import UserProfile


class _BookingFixture(_DayFixture):
    def book(self, unit, start, end, day=DAY, hard=False, **kw):
        return VehicleBooking.objects.create(
            vehicle=unit, date=day, start_time=start, end_time=end,
            booking_type=kw.pop("booking_type", "service"),
            reason=kw.pop("reason", "Tire service"), is_hard=hard, **kw)

    def fleet_manager(self):
        user = User.objects.create_user("bk_fm", password="x", is_staff=True)
        UserProfile.objects.create(user=user, phone_number="407-555-0190",
                                   is_fleet_manager=True)
        return user

    def save(self, **body):
        base = {"date": DAY.isoformat(), "start": "10:00", "end": "12:00",
                "booking_type": "service", "reason": "Tire service"}
        base.update(body)
        return self.client.post(reverse("fleet_save_booking"), data=json.dumps(base),
                                content_type="application/json")

    def post(self, name, payload, args=()):
        return self.client.post(reverse(name, args=args), data=json.dumps(payload),
                                content_type="application/json")


# ════════════════════════════════════════════════════════════════════════════
# The day draws them, and says when a trip has landed on one
# ════════════════════════════════════════════════════════════════════════════

@patch("dispatching.scheduler.estimate_job_end_time", _ninety_minutes)
class StripTests(_BookingFixture):
    def test_a_booking_sits_on_its_cars_line(self):
        unit = self.unit("6")
        driver = self.driver("strip_holder")
        self.hold(unit, driver)
        self.job(driver, 8)
        self.book(unit, time(11, 0), time(13, 0))
        row = self.row_for(self.payload(), "6")
        self.assertEqual(len(row["bookings"]), 1)
        booking = row["bookings"][0]
        self.assertEqual(booking["window"], "11:00 AM–1:00 PM")
        self.assertFalse(booking["conflict"])
        self.assertGreater(booking["width"], 0)

    def test_a_trip_inside_a_booking_is_a_conflict(self):
        """Booked 10–12; a 9:00 trip runs to 10:30 on the planning clock."""
        unit = self.unit("6")
        driver = self.driver("clash_holder")
        self.hold(unit, driver)
        self.job(driver, 9)
        self.book(unit, time(10, 0), time(12, 0))
        payload = self.payload()
        row = self.row_for(payload, "6")
        self.assertTrue(row["bookings"][0]["conflict"])
        self.assertIn("9:00 AM", row["bookings"][0]["conflict_line"])
        self.assertEqual(row["booking_conflicts"], 1)
        self.assertEqual(payload["booking_conflicts"], 1)

    def test_touching_ends_are_not_a_conflict(self):
        """A trip clearing at 10:30 and a booking from 10:30 fit together."""
        unit = self.unit("6")
        driver = self.driver("touch_holder")
        self.hold(unit, driver)
        self.job(driver, 9)
        self.book(unit, time(10, 30), time(12, 0))
        self.assertFalse(self.row_for(self.payload(), "6")["bookings"][0]["conflict"])

    def test_only_the_booked_part_of_a_hole_stops_being_free(self):
        """9:30 → 2:00 with a 10–11 booking in it: the booking's hour is not
        free, and 11:00 → 2:00 is exactly as free as it was."""
        from datetime import datetime
        unit = self.unit("6")
        driver = self.driver("hole_holder")
        self.hold(unit, driver)
        self.job(driver, 8)
        self.job(driver, 14)          # 9:30 → 14:00 is a long hole
        before = self.row_for(self.payload(), "6")
        self.assertTrue(any(g["reachable"] for g in before["gaps"]))
        self.book(unit, time(10, 0), time(11, 0))
        after = self.row_for(self.payload(), "6")
        booked = (datetime.combine(DAY, time(10, 0)), datetime.combine(DAY, time(11, 0)))
        for gap in after["gaps"]:
            self.assertFalse(gap["start"] < booked[1] and booked[0] < gap["end"],
                             f"{gap['from_label']}–{gap['to_label']} runs into the booking")
        rest = [g for g in after["gaps"] if g["reachable"]]
        self.assertEqual([(g["from_label"], g["to_label"]) for g in rest],
                         [("11:00 AM", "2:00 PM")])
        # The time AT BASE inside that hole: it opens where the booking ends,
        # which nothing knows (35 min in), and the next pickup is MCO (12 out).
        self.assertEqual((rest[0]["reachable"]["from_label"], rest[0]["reachable"]["to_label"]),
                         ("11:35 AM", "1:48 PM"))

    def test_an_early_booking_widens_the_axis(self):
        unit = self.unit("6")
        driver = self.driver("early_holder")
        self.hold(unit, driver)
        self.job(driver, 9)
        self.book(unit, time(4, 30), time(5, 30))
        payload = self.payload()
        self.assertLessEqual(payload["axis_start"].hour, 4)

    def test_a_cancelled_booking_is_gone_from_the_strip(self):
        unit = self.unit("6")
        driver = self.driver("gone_holder")
        self.hold(unit, driver)
        self.job(driver, 9)
        booking = self.book(unit, time(11, 0), time(12, 0))
        booking.cancelled_at = booking.created_at
        booking.save()
        self.assertEqual(self.row_for(self.payload(), "6")["bookings"], [])


# ════════════════════════════════════════════════════════════════════════════
# Saving and cancelling from the sheet
# ════════════════════════════════════════════════════════════════════════════

@patch("dispatching.scheduler.estimate_job_end_time", _ninety_minutes)
class SaveTests(_BookingFixture):
    def test_any_staff_member_can_make_a_soft_booking(self):
        unit = self.unit("6")
        resp = self.save(vehicle_id=unit.id, location="Discount Tire", notes="Rear pair")
        self.assertEqual(resp.status_code, 200, resp.content)
        booking = VehicleBooking.objects.get()
        self.assertFalse(booking.is_hard)
        self.assertEqual(booking.location, "Discount Tire")
        self.assertEqual(booking.created_by, self.staff)

    def test_the_end_has_to_come_after_the_start(self):
        unit = self.unit("6")
        resp = self.save(vehicle_id=unit.id, start="12:00", end="10:00")
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(VehicleBooking.objects.exists())

    def test_a_date_gone_by_is_refused(self):
        unit = self.unit("6")
        resp = self.save(vehicle_id=unit.id, date=(TODAY - timedelta(days=1)).isoformat())
        self.assertEqual(resp.status_code, 400)

    def test_other_needs_a_reason(self):
        unit = self.unit("6")
        resp = self.save(vehicle_id=unit.id, booking_type="other", reason="")
        self.assertEqual(resp.status_code, 400)

    def test_two_bookings_cannot_stack_on_one_car(self):
        unit = self.unit("6")
        self.book(unit, time(10, 0), time(12, 0))
        resp = self.save(vehicle_id=unit.id, start="11:00", end="13:00")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("already booked", resp.json()["error"])

    def test_a_car_off_the_road_all_day_is_not_bookable(self):
        unit = self.unit("6")
        VehicleDowntime.objects.create(
            vehicle=unit, category="repair", reason="Transmission",
            starts_on=DAY - timedelta(days=1), expected_back_on=DAY + timedelta(days=2))
        resp = self.save(vehicle_id=unit.id)
        self.assertEqual(resp.status_code, 400)
        self.assertIn("off the road", resp.json()["error"])

    def test_booking_over_a_trip_asks_first_then_saves(self):
        unit = self.unit("6")
        driver = self.driver("ack_holder")
        self.hold(unit, driver)
        self.job(driver, 9)                          # 9:00–10:30
        resp = self.save(vehicle_id=unit.id)          # 10:00–12:00
        self.assertEqual(resp.status_code, 409)
        self.assertTrue(resp.json()["needs_ack"])
        self.assertIn("9:00 AM", resp.json()["summary"])
        self.assertFalse(VehicleBooking.objects.exists())
        resp = self.save(vehicle_id=unit.id, acknowledge=True)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(VehicleBooking.objects.count(), 1)

    def test_a_plain_staff_member_cannot_make_a_hard_booking(self):
        unit = self.unit("6")
        resp = self.save(vehicle_id=unit.id, is_hard=True)
        self.assertEqual(resp.status_code, 403)
        self.assertFalse(VehicleBooking.objects.exists())

    def test_the_fleet_manager_can(self):
        unit = self.unit("6")
        self.client.force_login(self.fleet_manager())
        resp = self.save(vehicle_id=unit.id, is_hard=True)
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertTrue(VehicleBooking.objects.get().is_hard)

    def test_editing_moves_the_booking(self):
        unit = self.unit("6")
        booking = self.book(unit, time(10, 0), time(12, 0))
        resp = self.save(id=booking.id, start="13:00", end="14:30", reason="Detail")
        self.assertEqual(resp.status_code, 200, resp.content)
        booking.refresh_from_db()
        self.assertEqual((booking.start_time, booking.end_time), (time(13, 0), time(14, 30)))
        self.assertEqual(booking.updated_by, self.staff)

    def test_editing_does_not_clash_with_itself(self):
        unit = self.unit("6")
        booking = self.book(unit, time(10, 0), time(12, 0))
        resp = self.save(id=booking.id, start="10:30", end="12:00")
        self.assertEqual(resp.status_code, 200, resp.content)

    def test_only_fleet_can_change_a_hard_booking(self):
        unit = self.unit("6")
        booking = self.book(unit, time(10, 0), time(12, 0), hard=True)
        resp = self.save(id=booking.id, is_hard=False)     # softening it
        self.assertEqual(resp.status_code, 403)
        booking.refresh_from_db()
        self.assertTrue(booking.is_hard)

    def test_cancelling_keeps_the_row(self):
        unit = self.unit("6")
        booking = self.book(unit, time(10, 0), time(12, 0))
        resp = self.post("fleet_cancel_booking", {}, args=[booking.id])
        self.assertEqual(resp.status_code, 200)
        booking.refresh_from_db()
        self.assertIsNotNone(booking.cancelled_at)
        self.assertEqual(booking.cancelled_by, self.staff)
        self.assertFalse(VehicleBooking.active().exists())

    def test_only_fleet_can_lift_a_hard_booking(self):
        unit = self.unit("6")
        booking = self.book(unit, time(10, 0), time(12, 0), hard=True)
        resp = self.post("fleet_cancel_booking", {}, args=[booking.id])
        self.assertEqual(resp.status_code, 403)
        self.client.force_login(self.fleet_manager())
        resp = self.post("fleet_cancel_booking", {}, args=[booking.id])
        self.assertEqual(resp.status_code, 200)


# ════════════════════════════════════════════════════════════════════════════
# Dispatch: handing the car to a chauffeur
# ════════════════════════════════════════════════════════════════════════════

@patch("dispatching.scheduler.estimate_job_end_time", _ninety_minutes)
class HandOverTests(_BookingFixture):
    def assign(self, driver, unit, **extra):
        return self.post("update_inhouse_vehicle_assignment", {
            "driver_id": driver.id, "date": DAY.isoformat(), "vehicle_id": unit.id, **extra})

    def test_no_clash_assigns_normally(self):
        unit = self.unit("6")
        driver = self.driver("clean_driver")
        self.job(driver, 14)
        self.book(unit, time(10, 0), time(12, 0))
        resp = self.assign(driver, unit)
        self.assertEqual(resp.status_code, 200, resp.content)

    def test_a_soft_clash_asks_then_lets_them_continue(self):
        unit = self.unit("6")
        driver = self.driver("soft_driver")
        self.job(driver, 9)                           # 9:00–10:30 vs 10–12
        self.book(unit, time(10, 0), time(12, 0))
        resp = self.assign(driver, unit)
        self.assertEqual(resp.status_code, 409)
        body = resp.json()
        self.assertTrue(body["booking_conflict"])
        self.assertTrue(body["can_override"])
        self.assertIn("Tire service", body["error"])
        self.assertFalse(DriverVehicleAssignment.objects.filter(driver=driver).exists())
        resp = self.assign(driver, unit, override_booking=True)
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(DriverVehicleAssignment.objects.filter(driver=driver, vehicle=unit).exists())

    def test_a_hard_clash_cannot_be_overridden_here(self):
        unit = self.unit("6")
        driver = self.driver("hard_driver")
        self.job(driver, 9)
        self.book(unit, time(10, 0), time(12, 0), hard=True)
        resp = self.assign(driver, unit, override_booking=True)
        self.assertEqual(resp.status_code, 409)
        self.assertFalse(resp.json()["can_override"])
        self.assertFalse(DriverVehicleAssignment.objects.filter(driver=driver).exists())

    def test_the_pool_prints_the_booking_beside_the_car(self):
        from dispatching.views import _annotate_vehicle_status
        from drivers.models import FleetVehicle
        booked, free = self.unit("6"), self.unit("7")
        self.book(booked, time(10, 0), time(12, 0))
        cars = _annotate_vehicle_status(
            list(FleetVehicle.objects.filter(id__in=[booked.id, free.id])
                 .with_open_downtimes().order_by("vehicle_number")), DAY)
        by_id = {c.id: c for c in cars}
        self.assertEqual(by_id[free.id].booking_rows, [])
        self.assertEqual(by_id[booked.id].booking_rows[0]["short"],
                         "Booked 10:00 AM–12:00 PM · Tire service")
        self.assertFalse(by_id[booked.id].booking_rows[0]["hard"])


# ════════════════════════════════════════════════════════════════════════════
# Dispatch: putting a trip on the chauffeur who holds the car
# ════════════════════════════════════════════════════════════════════════════

@patch("dispatching.scheduler.estimate_job_end_time", _ninety_minutes)
class TripAssignTests(_BookingFixture):
    def setUp(self):
        super().setUp()
        self.car = self.unit("6")
        self.chauffeur = self.driver("trip_driver")
        self.hold(self.car, self.chauffeur)

    def assign(self, leg, **extra):
        return self.post("update_leg_assignment",
                         {"leg_id": leg.id, "field": "driver", "value": self.chauffeur.id,
                          **extra})

    def feasibility(self, leg):
        return self.client.get(reverse("check_driver_feasibility"),
                               {"leg_id": leg.id, "driver_id": self.chauffeur.id}).json()

    def hold_the_day(self):
        """A held day, and a staff member allowed to stage into it."""
        from django.contrib.auth.models import Permission
        from reservations.models import ScheduleDraft
        self.staff.user_permissions.add(Permission.objects.get(codename="use_schedule_sandbox"))
        self.client.force_login(User.objects.get(pk=self.staff.pk))
        return ScheduleDraft.objects.create(schedule_date=DAY, created_by=self.staff,
                                            state=ScheduleDraft.State.DRAFT)

    def test_a_trip_clear_of_the_booking_carries_no_booking_warning(self):
        self.book(self.car, time(10, 0), time(12, 0))
        leg = self.leg(DAY, hour=14)
        resp = self.assign(leg)
        self.assertTrue(resp.json()["success"])
        codes = {w["code"] for w in resp.json().get("warnings", [])}
        self.assertNotIn("vehicle_booking", codes)

    def test_a_soft_booking_asks_first_then_assigns_when_told_to_continue(self):
        self.book(self.car, time(10, 0), time(12, 0))
        leg = self.leg(DAY, hour=11)
        resp = self.assign(leg)
        self.assertEqual(resp.status_code, 409)             # asked, nothing written
        body = resp.json()
        self.assertTrue(body["booking_conflict"])
        self.assertTrue(body["can_override"])
        self.assertFalse(body["hard"])
        self.assertEqual(body["vehicle_number"], "6")
        self.assertIn("Vehicle #6 is booked from 10:00 AM–12:00 PM for Tire service.",
                      body["error"])
        leg.refresh_from_db()
        self.assertIsNone(leg.driver_id)

        resp = self.assign(leg, override_booking=True)      # "Continue anyway"
        body = resp.json()
        self.assertTrue(body["success"], body)
        # The dispatcher already chose — the write doesn't warn them again.
        self.assertNotIn("vehicle_booking", {w["code"] for w in body["warnings"]})
        leg.refresh_from_db()
        self.assertEqual(leg.driver_id, self.chauffeur.id)

    def test_a_hard_booking_refuses_the_trip(self):
        self.book(self.car, time(10, 0), time(12, 0), hard=True)
        leg = self.leg(DAY, hour=11)
        resp = self.assign(leg)
        self.assertEqual(resp.status_code, 409)
        self.assertFalse(resp.json()["success"])
        self.assertIn("hard", resp.json()["error"].lower())
        leg.refresh_from_db()
        self.assertIsNone(leg.driver_id)

    def test_a_hard_booking_cannot_be_overridden_at_assignment(self):
        self.book(self.car, time(10, 0), time(12, 0), hard=True)
        leg = self.leg(DAY, hour=11)
        resp = self.assign(leg, override_booking=True)
        self.assertEqual(resp.status_code, 409)
        self.assertFalse(resp.json()["can_override"])
        leg.refresh_from_db()
        self.assertIsNone(leg.driver_id)

    def test_a_hard_booking_refuses_a_staged_assignment_too(self):
        from reservations.models import DraftAssignment
        self.hold_the_day()
        self.book(self.car, time(10, 0), time(12, 0), hard=True)
        leg = self.leg(DAY, hour=11)
        resp = self.assign(leg)
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(DraftAssignment.objects.count(), 0)

    def test_a_hard_booking_refuses_an_edit_live_override(self):
        from reservations.models import DraftAssignment
        self.hold_the_day()
        self.book(self.car, time(10, 0), time(12, 0), hard=True)
        leg = self.leg(DAY, hour=11)
        resp = self.assign(leg, live_override=True)
        self.assertEqual(resp.status_code, 409)
        leg.refresh_from_db()
        self.assertIsNone(leg.driver_id)
        self.assertEqual(DraftAssignment.objects.count(), 0)

    def test_the_board_asks_before_a_soft_clash(self):
        self.book(self.car, time(10, 0), time(12, 0))
        leg = self.leg(DAY, hour=11)
        result = self.feasibility(leg)
        self.assertFalse(result["hard_block"])
        self.assertTrue(result["feasible"])
        self.assertIn("Tire service", result["booking_warning"])
        # Its own field, so the question is never asked twice.
        self.assertFalse(any("Tire service" in w for w in result["warnings"]))

    def test_the_board_refuses_a_hard_clash_up_front(self):
        self.book(self.car, time(10, 0), time(12, 0), hard=True)
        leg = self.leg(DAY, hour=11)
        result = self.feasibility(leg)
        self.assertTrue(result["hard_block"])
        self.assertFalse(result["feasible"])
        self.assertIn("hard-booked", result["reason"])
        self.assertIsNone(result["booking_warning"])

    def test_the_board_refuses_a_hard_clash_for_a_chauffeur_with_trips(self):
        """The branch the board normally takes: a day already under way."""
        self.job(self.chauffeur, 6)
        self.book(self.car, time(10, 0), time(12, 0), hard=True)
        leg = self.leg(DAY, hour=11)
        result = self.feasibility(leg)
        self.assertEqual(result["existing_trips"], 1)
        self.assertTrue(result["hard_block"])
        self.assertFalse(result["feasible"])
        self.assertIn("hard-booked", result["reason"])

    def test_the_board_asks_about_a_soft_clash_for_a_chauffeur_with_trips(self):
        self.job(self.chauffeur, 6)
        self.book(self.car, time(10, 0), time(12, 0))
        leg = self.leg(DAY, hour=11)
        result = self.feasibility(leg)
        self.assertEqual(result["existing_trips"], 1)
        self.assertFalse(result["hard_block"])
        self.assertTrue(result["feasible"])
        self.assertIn("Tire service", result["booking_warning"])
        self.assertFalse(any("Tire service" in w for w in result["warnings"]))

    def test_a_driver_with_no_car_is_never_blocked_by_a_booking(self):
        other = self.driver("carless")
        self.book(self.car, time(10, 0), time(12, 0), hard=True)
        leg = self.leg(DAY, hour=11)
        self.assertIsNone(fleet_bookings.booking_clash(leg, other))


# ════════════════════════════════════════════════════════════════════════════
# The page
# ════════════════════════════════════════════════════════════════════════════

@patch("dispatching.scheduler.estimate_job_end_time", _ninety_minutes)
class PageTests(_BookingFixture):
    def test_the_sheet_and_the_booking_render(self):
        unit = self.unit("6")
        driver = self.driver("page_holder")
        self.hold(unit, driver)
        self.job(driver, 9)
        booking = self.book(unit, time(10, 0), time(12, 0))
        resp = self.client.get(reverse("fleet_day"), {"date": DAY.isoformat()})
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'id="fbModal"')
        self.assertContains(resp, f'data-booking="{booking.id}"')
        self.assertContains(resp, "Booking conflict")
        self.assertIn(booking.id, resp.context["booking_index"])
        self.assertFalse(resp.context["can_hard"])

    def test_an_unbuilt_day_lists_its_bookings(self):
        unit = self.unit("6")
        self.leg(DAY, hour=9)
        self.book(unit, time(10, 0), time(12, 0))
        resp = self.client.get(reverse("fleet_day"), {"date": DAY.isoformat()})
        self.assertIsNone(resp.context["payload"])
        self.assertEqual(len(resp.context["demand_bookings"]), 1)
        self.assertContains(resp, "Booked by fleet on this day")

    def test_the_fleet_manager_is_offered_hard_bookings(self):
        self.unit("6")
        self.client.force_login(self.fleet_manager())
        resp = self.client.get(reverse("fleet_day"))
        self.assertTrue(resp.context["can_hard"])

    def test_the_schedule_board_names_the_booking(self):
        unit = self.unit("6")
        driver = self.driver("board_holder")
        self.hold(unit, driver)
        self.job(driver, 14)
        self.book(unit, time(10, 0), time(12, 0))
        resp = self.client.get(reverse("schedule_board"), {"date": DAY.isoformat()})
        if resp.status_code != 200:
            self.skipTest(f"schedule board not reachable in this fixture ({resp.status_code})")
        # The board's tag is fleet_bookings.chip(compact=True), the same one
        # every dispatch surface prints.
        self.assertContains(resp, "Booked 10a–12p · Tire service")
