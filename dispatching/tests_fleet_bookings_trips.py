"""Vehicle bookings on every path that puts a TRIP on a chauffeur.

Run with:  ENABLE_DEBUG_TOOLBAR=0 ./manage.py test dispatching.tests_fleet_bookings_trips

A HARD booking means no trip lands on that car in that window, whichever screen
the dispatcher is on and whether the day is live or held — so every path gets a
test proving the refusal writes nothing: the leg's driver is unchanged and no
draft row is staged. A SOFT booking is a question on the interactive paths
(409 with ``can_override`` until the caller resends ``override_booking``) and a
disclosure on the bulk ones (applied, and listed back).

Same fixture as tests_fleet_bookings: car #6 held by one chauffeur, trip ends
pinned to pickup + 90 minutes, so an 11:00 trip runs 11:00–12:30 and a 9:00
trip clears at 10:30.
"""
from datetime import time
from unittest.mock import patch

from django.contrib.auth.models import Permission, User
from django.urls import reverse
from django.utils import timezone

from dispatching import board_validation, fleet_bookings
from dispatching.tests_fleet_bookings import _BookingFixture
from dispatching.tests_fleet_day import _ninety_minutes
from dispatching.tests_fleet_desk import DAY
from drivers.models import DriverVehicleAssignment
from reservations.models import DraftAssignment, Leg, ScheduleDraft


class _TripFixture(_BookingFixture):
    def setUp(self):
        super().setUp()
        self.car = self.unit("6")
        self.chauffeur = self.driver("trip_holder")
        self.hold(self.car, self.chauffeur)

    def hard(self, start=time(10, 0), end=time(12, 0), unit=None):
        return self.book(unit or self.car, start, end, hard=True)

    def soft(self, start=time(10, 0), end=time(12, 0), unit=None):
        return self.book(unit or self.car, start, end)

    def hold_the_day(self):
        """A held day, and the staff member allowed to stage into it."""
        self.staff.user_permissions.add(Permission.objects.get(codename="use_schedule_sandbox"))
        self.client.force_login(User.objects.get(pk=self.staff.pk))
        return ScheduleDraft.objects.create(schedule_date=DAY, created_by=self.staff,
                                            state=ScheduleDraft.State.DRAFT)

    def driver_of(self, leg):
        return Leg.objects.values_list("driver_id", flat=True).get(pk=leg.pk)

    def assert_untouched(self, leg, driver_id=None):
        self.assertEqual(self.driver_of(leg), driver_id)
        self.assertFalse(DraftAssignment.objects.filter(leg=leg).exists())


# ════════════════════════════════════════════════════════════════════════════
# The single-trip paths: dropdowns, the board, the Swap Tester's takeback
# ════════════════════════════════════════════════════════════════════════════

@patch("dispatching.scheduler.estimate_job_end_time", _ninety_minutes)
class TakebackTests(_TripFixture):
    def takeback(self, leg, **extra):
        return self.post("execute_takeback", {"leg_id": leg.id, "driver_id": self.chauffeur.id,
                                              "date": DAY.isoformat(), **extra})

    def test_a_hard_booking_refuses_the_takeback(self):
        self.hard()
        leg = self.leg(DAY, hour=11)
        resp = self.takeback(leg, override_booking=True)
        self.assertEqual(resp.status_code, 409)
        self.assertTrue(resp.json()["hard"])
        self.assertFalse(resp.json()["can_override"])
        self.assert_untouched(leg)

    def test_a_hard_booking_refuses_a_staged_takeback(self):
        self.hold_the_day()
        self.hard()
        leg = self.leg(DAY, hour=11)
        self.assertEqual(self.takeback(leg).status_code, 409)
        self.assert_untouched(leg)

    def test_a_soft_booking_asks_then_takes_back_on_continue(self):
        self.soft()
        leg = self.leg(DAY, hour=11)
        resp = self.takeback(leg)
        self.assertEqual(resp.status_code, 409)
        self.assertTrue(resp.json()["can_override"])
        self.assert_untouched(leg)
        resp = self.takeback(leg, override_booking=True)
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(self.driver_of(leg), self.chauffeur.id)


@patch("dispatching.scheduler.estimate_job_end_time", _ninety_minutes)
class EdgeTests(_TripFixture):
    """Touching ends, cancelled bookings, and a car two chauffeurs share."""

    def assign(self, leg, driver=None, **extra):
        return self.post("update_leg_assignment", {
            "leg_id": leg.id, "field": "driver", "value": (driver or self.chauffeur).id, **extra})

    def test_a_trip_clearing_as_the_booking_starts_is_not_a_clash(self):
        self.hard(time(10, 30), time(12, 0))
        leg = self.leg(DAY, hour=9)                         # 9:00–10:30
        resp = self.assign(leg)
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(self.driver_of(leg), self.chauffeur.id)

    def test_a_cancelled_hard_booking_refuses_nothing(self):
        booking = self.hard()
        booking.cancelled_at = timezone.now()
        booking.save()
        leg = self.leg(DAY, hour=11)
        feas = self.client.get(reverse("check_driver_feasibility"),
                               {"leg_id": leg.id, "driver_id": self.chauffeur.id}).json()
        self.assertFalse(feas["hard_block"])
        self.assertIsNone(feas["booking_warning"])
        resp = self.assign(leg)
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(self.driver_of(leg), self.chauffeur.id)

    def test_the_co_driver_of_a_hard_booked_car_is_refused_too(self):
        partner = self.driver("trip_partner")
        self.hold(self.car, partner)
        self.hard()
        leg = self.leg(DAY, hour=11)
        resp = self.assign(leg, driver=partner)
        self.assertEqual(resp.status_code, 409)
        self.assertFalse(resp.json()["can_override"])
        self.assert_untouched(leg)

    def test_the_co_driver_of_a_soft_booked_car_is_asked(self):
        partner = self.driver("trip_partner")
        self.hold(self.car, partner)
        self.soft()
        leg = self.leg(DAY, hour=11)
        self.assertEqual(self.assign(leg, driver=partner).status_code, 409)
        resp = self.assign(leg, driver=partner, override_booking=True)
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(self.driver_of(leg), partner.id)

    def test_a_partners_trip_in_the_booking_does_not_block_a_clear_trip(self):
        partner = self.driver("trip_partner")
        self.hold(self.car, partner)
        self.job(self.chauffeur, 11)                        # already inside the booking
        self.hard()
        leg = self.leg(DAY, hour=14)
        resp = self.assign(leg, driver=partner)
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(self.driver_of(leg), partner.id)


# ════════════════════════════════════════════════════════════════════════════
# Swaps (planner Find Swaps, Swap Tester)
# ════════════════════════════════════════════════════════════════════════════

@patch("dispatching.scheduler.estimate_job_end_time", _ninety_minutes)
class SwapTests(_TripFixture):
    def swap(self, *moves, **extra):
        return self.post("execute_swap", {
            "date": DAY.isoformat(),
            "moves": [{"leg_id": leg.id, "to_driver_id": driver.id} for leg, driver in moves],
            **extra})

    def test_a_hard_booking_refuses_the_whole_swap(self):
        other = self.driver("swap_other")
        self.hold(self.unit("7"), other)
        self.hard()
        into_booking = self.leg(DAY, hour=11)
        elsewhere = self.leg(DAY, hour=15)
        resp = self.swap((elsewhere, other), (into_booking, self.chauffeur),
                         override_booking=True)
        self.assertEqual(resp.status_code, 409)
        body = resp.json()
        self.assertFalse(body["can_override"])
        self.assertEqual(body["leg_id"], into_booking.id)
        self.assertIn("The 11:00 AM trip", body["error"])
        self.assert_untouched(into_booking)
        self.assert_untouched(elsewhere)                    # nothing else moved either

    def test_a_hard_booking_refuses_a_staged_swap(self):
        self.hold_the_day()
        self.hard()
        leg = self.leg(DAY, hour=11)
        resp = self.swap((leg, self.chauffeur))
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(DraftAssignment.objects.count(), 0)
        self.assert_untouched(leg)

    def test_soft_bookings_are_asked_about_together_then_applied(self):
        other = self.driver("swap_other")
        car7 = self.unit("7")
        self.hold(car7, other)
        self.soft()
        self.soft(unit=car7)
        first, second = self.leg(DAY, hour=11), self.leg(DAY, hour=10)
        resp = self.swap((first, self.chauffeur), (second, other))
        self.assertEqual(resp.status_code, 409)
        body = resp.json()
        self.assertTrue(body["can_override"])
        self.assertIn("Vehicle #6", body["error"])          # every soft clash listed
        self.assertIn("Vehicle #7", body["error"])
        self.assertEqual({w["leg_id"] for w in body["booking_warnings"]},
                         {first.id, second.id})
        self.assert_untouched(first)
        with patch("dispatching.views._revalidate_swap_feasibility", return_value=(True, "")):
            resp = self.swap((first, self.chauffeur), (second, other), override_booking=True)
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(self.driver_of(first), self.chauffeur.id)
        self.assertEqual(self.driver_of(second), other.id)

    def test_revalidation_refuses_a_move_into_a_hard_booking(self):
        """The accept side of Find Swaps / the Recovery Advisor's DB check."""
        self.hard()
        leg = self.leg(DAY, hour=11)
        ok, reason = board_validation.revalidate_moves_against_db(
            [(leg.id, self.chauffeur.id)], DAY)
        self.assertFalse(ok)
        self.assertIn("hard-booked", reason)


# ════════════════════════════════════════════════════════════════════════════
# The builders: Smart Schedule Builder, auto-assign
# ════════════════════════════════════════════════════════════════════════════

@patch("dispatching.scheduler.estimate_job_end_time", _ninety_minutes)
class SmartBuilderTests(_TripFixture):
    def build(self, apply=False):
        return self.post("smart_schedule_builder", {
            "driver_id": self.chauffeur.id, "date": DAY.isoformat(),
            "start_hour": 0, "end_hour": 23, "apply": apply})

    def test_a_hard_booked_leg_is_named_in_the_preview_and_skipped_on_apply(self):
        self.hard()
        leg = self.leg(DAY, hour=11)
        clear = self.leg(DAY, hour=15)
        preview = self.build().json()
        self.assertIn(leg.id, [s["leg_id"] for s in preview["schedule"]])
        warning = next(w for w in preview["booking_warnings"] if w["leg_id"] == leg.id)
        self.assertTrue(warning["hard"])
        self.assertEqual(warning["driver_id"], self.chauffeur.id)

        body = self.build(apply=True).json()
        self.assertEqual(body["refused_booking"][0]["leg_id"], leg.id)
        self.assertIn("hard-booked", body["refused_booking"][0]["text"])
        self.assertEqual(body["assigned_count"], 1)          # the clear one only
        self.assert_untouched(leg)
        self.assertEqual(self.driver_of(clear), self.chauffeur.id)

    def test_a_hard_booked_leg_is_not_staged_either(self):
        self.hold_the_day()
        self.hard()
        leg = self.leg(DAY, hour=11)
        body = self.build(apply=True).json()
        self.assertEqual([r["leg_id"] for r in body["refused_booking"]], [leg.id])
        self.assert_untouched(leg)

    def test_a_soft_booked_leg_is_disclosed_and_applied(self):
        self.soft()
        leg = self.leg(DAY, hour=11)
        body = self.build(apply=True).json()
        self.assertEqual(body["refused_booking"], [])
        self.assertFalse(next(w for w in body["booking_warnings"]
                              if w["leg_id"] == leg.id)["hard"])
        self.assertEqual(self.driver_of(leg), self.chauffeur.id)


@patch("dispatching.scheduler.estimate_job_end_time", _ninety_minutes)
class AutoAssignTests(_TripFixture):
    def run_it(self, leg, apply=False):
        return self.post("auto_assign_drivers", {
            "date": DAY.isoformat(), "apply": apply,
            "driver_hours": {str(self.chauffeur.id): {"start": 0, "end": 23, "flexible": True}},
            "manual_assignments": {str(leg.id): self.chauffeur.id},
        }).json()

    def test_a_hard_booked_pin_is_named_in_the_preview(self):
        self.hard()
        leg = self.leg(DAY, hour=11)
        body = self.run_it(leg)
        self.assertEqual(body["booking_warnings"],
                         [{"leg_id": leg.id, "driver_id": self.chauffeur.id, "hard": True,
                           "text": body["booking_warnings"][0]["text"]}])
        self.assertIn("The 11:00 AM trip", body["booking_warnings"][0]["text"])

    def test_apply_leaves_a_hard_booked_pin_unassigned(self):
        self.hard()
        leg = self.leg(DAY, hour=11)
        body = self.run_it(leg, apply=True)
        self.assertTrue(body["success"], body)
        self.assertEqual(body["assigned"], 0)
        self.assertEqual([r["leg_id"] for r in body["refused_booking"]], [leg.id])
        self.assert_untouched(leg)

    def test_apply_does_not_stage_a_hard_booked_pin_on_a_held_day(self):
        self.hold_the_day()
        self.hard()
        leg = self.leg(DAY, hour=11)
        body = self.run_it(leg, apply=True)
        self.assertTrue(body["held"])
        self.assertEqual([r["leg_id"] for r in body["refused_booking"]], [leg.id])
        self.assert_untouched(leg)

    def test_apply_writes_a_soft_booked_pin_and_says_so(self):
        self.soft()
        leg = self.leg(DAY, hour=11)
        body = self.run_it(leg, apply=True)
        self.assertEqual(body["refused_booking"], [])
        self.assertEqual([w["leg_id"] for w in body["booking_warnings"]], [leg.id])
        self.assertEqual(self.driver_of(leg), self.chauffeur.id)


# ════════════════════════════════════════════════════════════════════════════
# The held day's publish, and snapshot restore
# ════════════════════════════════════════════════════════════════════════════

@patch("dispatching.scheduler.estimate_job_end_time", _ninety_minutes)
class PublishTests(_TripFixture):
    def setUp(self):
        super().setUp()
        from dispatching.assignment import _upsert_draft_assignment
        self.draft = self.hold_the_day()
        self.leg_ = self.leg(DAY, hour=11)
        # Staged before fleet booked the car — the case publish must catch.
        _upsert_draft_assignment(self.draft, self.leg_, self.chauffeur, self.staff)
        self.client.force_login(User.objects.create_superuser("bk_manager", password="x"))

    def publish(self, **extra):
        return self.post("publish_draft", {"draft_id": self.draft.id, **extra})

    def test_a_hard_booking_made_after_staging_blocks_the_publish(self):
        self.hard()
        resp = self.publish()
        self.assertEqual(resp.status_code, 409)
        body = resp.json()
        self.assertEqual([c["leg_id"] for c in body["booking_conflicts"]], [self.leg_.id])
        self.assertIn("hard-booked", body["error"])
        self.assertIsNone(self.driver_of(self.leg_))

    def test_force_does_not_get_past_a_hard_booking(self):
        self.hard()
        self.assertEqual(self.publish(force=True).status_code, 409)
        self.assertIsNone(self.driver_of(self.leg_))
        self.draft.refresh_from_db()
        self.assertNotEqual(self.draft.state, ScheduleDraft.State.PUBLISHED)

    def test_a_soft_booking_publishes_and_is_listed(self):
        self.soft()
        resp = self.publish()
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual([w["leg_id"] for w in resp.json()["booking_warnings"]], [self.leg_.id])
        self.assertEqual(self.driver_of(self.leg_), self.chauffeur.id)


@patch("dispatching.scheduler.estimate_job_end_time", _ninety_minutes)
class RestoreTests(_TripFixture):
    def snapshot_then_clear(self, leg):
        from dispatching.views import _create_schedule_snapshot
        snapshot = _create_schedule_snapshot(DAY, self.staff, "manual")
        Leg.objects.filter(pk=leg.pk).update(driver=None)
        return snapshot

    def restore(self, snapshot):
        return self.post("restore_schedule_snapshot", {"snapshot_id": snapshot.id})

    def test_a_snapshot_from_before_a_hard_booking_leaves_that_trip_alone(self):
        leg = self.job(self.chauffeur, 11)
        clear = self.job(self.chauffeur, 15)
        snapshot = self.snapshot_then_clear(leg)
        self.hard()
        body = self.restore(snapshot).json()
        self.assertEqual([s["leg_id"] for s in body["skipped_booking"]], [leg.id])
        self.assertEqual(body["restored"], 1)
        self.assertIsNone(self.driver_of(leg))
        self.assertEqual(self.driver_of(clear), self.chauffeur.id)

    def test_nor_is_it_restored_into_a_draft(self):
        leg = self.job(self.chauffeur, 11)
        snapshot = self.snapshot_then_clear(leg)
        self.hold_the_day()
        self.hard()
        body = self.restore(snapshot).json()
        self.assertTrue(body["held"])
        self.assertEqual([s["leg_id"] for s in body["skipped_booking"]], [leg.id])
        self.assert_untouched(leg)

    def test_a_soft_booking_restores(self):
        leg = self.job(self.chauffeur, 11)
        snapshot = self.snapshot_then_clear(leg)
        self.soft()
        body = self.restore(snapshot).json()
        self.assertEqual(body["skipped_booking"], [])
        self.assertEqual(self.driver_of(leg), self.chauffeur.id)


# ════════════════════════════════════════════════════════════════════════════
# The Recovery Advisor and the Farm-Out Optimizer applies
# ════════════════════════════════════════════════════════════════════════════

@patch("dispatching.scheduler.estimate_job_end_time", _ninety_minutes)
class AdvisorApplyTests(_TripFixture):
    def apply(self, actions, expected, expected_times=None):
        from dispatching.conflict_advisor_actions import apply_advisor_plan
        return apply_advisor_plan({
            "schema": 1, "date": DAY.isoformat(), "disruption_id": "overlap:1:2",
            "plan_id": "overlap:1:2#p1", "task_id": None, "actions": actions,
            "expected": {str(k): v for k, v in expected.items()},
            "expected_times": {str(k): v for k, v in (expected_times or {}).items()},
        }, self.staff)

    def test_a_reassign_into_a_hard_booking_is_refused(self):
        self.hard()
        leg = self.leg(DAY, hour=11)
        status, body = self.apply(
            [{"op": "reassign", "leg_id": leg.id, "to_driver_id": self.chauffeur.id}],
            {leg.id: None})
        self.assertEqual(status, 409)
        self.assertIn("hard-booked", body["error"])
        self.assert_untouched(leg)

    def test_a_retime_into_a_hard_booking_is_refused(self):
        self.hard()
        leg = self.job(self.chauffeur, 14)                  # clear of 10–12 today
        status, body = self.apply(
            [{"op": "retime", "leg_id": leg.id, "new_pickup_time": "10:30"}],
            {leg.id: self.chauffeur.id}, {leg.id: "14:00"})
        self.assertEqual(status, 409)
        self.assertIn("The 10:30 AM trip", body["error"])
        leg.refresh_from_db()
        self.assertEqual(leg.pickup_time, time(14, 0))

    def test_a_retime_into_a_soft_booking_applies_with_a_warning(self):
        self.soft()
        leg = self.job(self.chauffeur, 14)
        status, body = self.apply(
            [{"op": "retime", "leg_id": leg.id, "new_pickup_time": "10:30"}],
            {leg.id: self.chauffeur.id}, {leg.id: "14:00"})
        self.assertEqual(status, 200, body)
        self.assertTrue(any("Tire service" in w for w in body["warnings"]))
        leg.refresh_from_db()
        self.assertEqual(leg.pickup_time, time(10, 30))


@patch("dispatching.scheduler.estimate_job_end_time", _ninety_minutes)
class FarmoutApplyTests(_TripFixture):
    def rescue(self, leg):
        return self.post("farmout_apply", {
            "kind": "free_rescue", "date": DAY.isoformat(), "target_leg_id": leg.id,
            "keep_driver_id": self.chauffeur.id, "moves": [[leg.id, self.chauffeur.id]],
            "expected": {str(leg.id): None}})

    def test_keeping_a_trip_in_house_on_a_hard_booked_car_is_refused(self):
        self.hard()
        leg = self.leg(DAY, hour=11)
        resp = self.rescue(leg)
        self.assertEqual(resp.status_code, 409)
        self.assertIn("hard-booked", resp.json()["error"])
        self.assert_untouched(leg)

    def test_nor_staged(self):
        self.hold_the_day()
        self.hard()
        leg = self.leg(DAY, hour=11)
        self.assertEqual(self.rescue(leg).status_code, 409)
        self.assert_untouched(leg)

    def test_a_soft_booking_applies_and_is_disclosed(self):
        self.soft()
        leg = self.leg(DAY, hour=11)
        resp = self.rescue(leg)
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual([w["leg_id"] for w in resp.json()["booking_warnings"]], [leg.id])
        self.assertEqual(self.driver_of(leg), self.chauffeur.id)


# ════════════════════════════════════════════════════════════════════════════
# The in-memory board check, the front door, and the admin backstop
# ════════════════════════════════════════════════════════════════════════════

@patch("dispatching.scheduler.estimate_job_end_time", _ninety_minutes)
class BoardAndFrontDoorTests(_TripFixture):
    def test_the_board_check_rejects_a_move_into_a_hard_booking_when_given_them(self):
        from dispatching.scheduler import build_driver_schedules
        from drivers.models import Driver
        self.hard()
        leg = self.leg(DAY, hour=11)
        schedules = build_driver_schedules([], [Driver.objects.get(pk=self.chauffeur.pk)], DAY)
        kwargs = dict(windows={}, sharer_partners={}, baseline_bands={})
        without = board_validation.validate_post_move_board(
            schedules, {leg.id: leg}, [(leg.id, self.chauffeur.id)], DAY, **kwargs)
        self.assertTrue(without.ok, without.reason)          # opt-in: query-free by default
        booked = board_validation.bookings_by_driver(DAY)
        self.assertEqual(list(booked), [self.chauffeur.id])
        verdict = board_validation.validate_post_move_board(
            schedules, {leg.id: leg}, [(leg.id, self.chauffeur.id)], DAY,
            booked=booked, **kwargs)
        self.assertFalse(verdict.ok)
        self.assertIn("hard-booked", verdict.reason)

    def test_the_front_door_refuses_before_staging_or_writing(self):
        from dispatching.assignment import set_leg_driver
        self.hard()
        leg = self.leg(DAY, hour=11)
        with self.assertRaises(fleet_bookings.HardBookingRefused):
            set_leg_driver(leg, self.chauffeur, self.staff)
        self.assert_untouched(leg)


@patch("dispatching.scheduler.estimate_job_end_time", _ninety_minutes)
class AdminTests(_TripFixture):
    def forms(self):
        from django.contrib import admin
        from django.test import RequestFactory
        from reservations.admin import LegAdminForm
        request = RequestFactory().get("/")
        request.user = self.staff
        changelist = admin.site._registry[Leg].get_changelist_form(request, fields=["driver"])
        return changelist, LegAdminForm

    def test_the_changelist_refuses_a_driver_into_a_hard_booking(self):
        self.hard()
        leg = self.leg(DAY, hour=11)
        changelist, _ = self.forms()
        form = changelist(data={"driver": self.chauffeur.id}, instance=leg)
        self.assertFalse(form.is_valid())
        self.assertIn("hard-booked", " ".join(form.errors["driver"]))

    def test_the_changelist_lets_a_soft_booking_through(self):
        self.soft()
        leg = self.leg(DAY, hour=11)
        changelist, _ = self.forms()
        form = changelist(data={"driver": self.chauffeur.id}, instance=leg)
        self.assertTrue(form.is_valid(), form.errors)

    def test_the_change_form_refuses_it_too(self):
        from django.forms import modelform_factory
        self.hard()
        leg = self.leg(DAY, hour=11)
        _, base = self.forms()
        form_class = modelform_factory(Leg, form=base, fields=["driver", "pickup_date", "pickup_time"])
        form = form_class(data={"driver": self.chauffeur.id, "pickup_date": DAY.isoformat(),
                                "pickup_time": "11:00"}, instance=leg)
        self.assertFalse(form.is_valid())
        self.assertIn("driver", form.errors)
        # Moved clear of the booking in the same save, the change is fine.
        form = form_class(data={"driver": self.chauffeur.id, "pickup_date": DAY.isoformat(),
                                "pickup_time": "14:00"}, instance=leg)
        self.assertTrue(form.is_valid(), form.errors)
