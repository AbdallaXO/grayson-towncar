"""Vehicle bookings on every path that hands a CAR to a chauffeur for the day,
and on what dispatch sees beside a car.

  * the single pool drop (update_inhouse_vehicle_assignment), incl. the
    one-step move off another chauffeur's card (``from_driver_id``);
  * copy yesterday's cars (copy_vehicle_assignments);
  * Day Setup — the suggester's notes and the Apply write;
  * the booking tags: schedule board rows, the pools and the assigned-car chips
    on the legs dashboard and the planner, and the trip dropdown labels.

Run with:  ENABLE_DEBUG_TOOLBAR=0 ./manage.py test dispatching.tests_fleet_bookings_cars

Trip ends are pinned to pickup + 90 minutes (the tests_fleet_day trick), so a
clash is arithmetic: a 9:00 trip occupies 9:00–10:30, an 11:00 one 11:00–12:30.
"""
from datetime import time, timedelta
from unittest.mock import patch

from django.urls import reverse
from django.utils import timezone

from dispatching import day_setup
from dispatching.tests_fleet_bookings import _BookingFixture
from dispatching.tests_fleet_day import _ninety_minutes
from dispatching.tests_fleet_desk import DAY
from drivers.models import DriverVehicleAssignment

PREV = DAY - timedelta(days=1)


def _cancel(booking):
    booking.cancelled_at = timezone.now()
    booking.save(update_fields=["cancelled_at"])
    return booking


def _held(driver, day=DAY):
    row = DriverVehicleAssignment.objects.filter(driver=driver, date=day).first()
    return row.vehicle_id if row else None


# ════════════════════════════════════════════════════════════════════════════
# The single pool drop
# ════════════════════════════════════════════════════════════════════════════

@patch("dispatching.scheduler.estimate_job_end_time", _ninety_minutes)
class PoolDropTests(_BookingFixture):
    def drop(self, driver, unit, **extra):
        return self.post("update_inhouse_vehicle_assignment", {
            "driver_id": driver.id, "date": DAY.isoformat(), "vehicle_id": unit.id, **extra})

    def test_the_refusal_is_the_shared_409_body(self):
        unit = self.unit("6")
        driver = self.driver("body_driver")
        self.job(driver, 11)
        booking = self.book(unit, time(10, 0), time(12, 0))
        resp = self.drop(driver, unit)
        self.assertEqual(resp.status_code, 409)
        body = resp.json()
        self.assertEqual(
            {k: body[k] for k in ("success", "booking_conflict", "hard", "can_override",
                                  "booking_id", "vehicle_number")},
            {"success": False, "booking_conflict": True, "hard": False,
             "can_override": True, "booking_id": booking.id, "vehicle_number": "6"})
        self.assertIn("Tire service", body["error"])

    def test_a_cancelled_hard_booking_does_not_block_the_drop(self):
        unit = self.unit("6")
        driver = self.driver("cancel_driver")
        self.job(driver, 11)
        _cancel(self.book(unit, time(10, 0), time(12, 0), hard=True))
        resp = self.drop(driver, unit)
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(_held(driver), unit.id)

    # ── shared car: only the RECEIVER's trips count ──────────────────────────

    def test_a_co_drivers_trip_inside_the_booking_does_not_block_the_receiver(self):
        unit = self.unit("6")
        first, second = self.driver("share_first"), self.driver("share_second")
        self.hold(unit, first)
        self.job(first, 11)                    # inside the hard window, on HIS day
        self.job(second, 14)                   # clear of it
        self.book(unit, time(10, 0), time(12, 0), hard=True)
        resp = self.drop(second, unit)
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(_held(second), unit.id)
        self.assertEqual(_held(first), unit.id)

    def test_the_receivers_own_trip_inside_it_still_does(self):
        unit = self.unit("6")
        first, second = self.driver("share_one"), self.driver("share_two")
        self.hold(unit, first)
        self.job(second, 11)
        self.book(unit, time(10, 0), time(12, 0), hard=True)
        resp = self.drop(second, unit, override_booking=True)
        self.assertEqual(resp.status_code, 409)
        self.assertIsNone(_held(second))

    # ── the one-step move off another chauffeur's card ─────────────────────

    def test_a_move_clears_the_donor_and_saves_the_receiver_together(self):
        unit = self.unit("6")
        donor, receiver = self.driver("move_donor"), self.driver("move_receiver")
        self.hold(unit, donor)
        self.job(receiver, 14)
        self.book(unit, time(10, 0), time(12, 0), hard=True)
        resp = self.drop(receiver, unit, from_driver_id=donor.id)
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.json()["cleared_driver_id"], donor.id)
        self.assertIsNone(_held(donor))
        self.assertEqual(_held(receiver), unit.id)

    def test_a_refused_move_leaves_the_donor_holding_the_car(self):
        unit = self.unit("6")
        donor, receiver = self.driver("keep_donor"), self.driver("keep_receiver")
        self.hold(unit, donor)
        self.job(receiver, 11)
        self.book(unit, time(10, 0), time(12, 0), hard=True)
        resp = self.drop(receiver, unit, from_driver_id=donor.id, override_booking=True)
        self.assertEqual(resp.status_code, 409)
        self.assertFalse(resp.json()["can_override"])
        self.assertEqual(_held(donor), unit.id)
        self.assertIsNone(_held(receiver))

    def test_a_soft_move_asks_first_and_moves_on_continue(self):
        unit = self.unit("6")
        donor, receiver = self.driver("soft_donor"), self.driver("soft_receiver")
        self.hold(unit, donor)
        self.job(receiver, 11)
        self.book(unit, time(10, 0), time(12, 0))
        resp = self.drop(receiver, unit, from_driver_id=donor.id)
        self.assertEqual(resp.status_code, 409)
        self.assertTrue(resp.json()["can_override"])
        self.assertEqual(_held(donor), unit.id)          # untouched while asking
        resp = self.drop(receiver, unit, from_driver_id=donor.id, override_booking=True)
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertIsNone(_held(donor))
        self.assertEqual(_held(receiver), unit.id)

    def test_a_donor_whose_card_changed_meanwhile_is_left_alone(self):
        unit, other = self.unit("6"), self.unit("7")
        donor, receiver = self.driver("stale_donor"), self.driver("stale_receiver")
        self.hold(other, donor)                # the drag was of #6; he now has #7
        resp = self.drop(receiver, unit, from_driver_id=donor.id)
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertIsNone(resp.json()["cleared_driver_id"])
        self.assertEqual(_held(donor), other.id)
        self.assertEqual(_held(receiver), unit.id)


# ════════════════════════════════════════════════════════════════════════════
# Copy yesterday's cars
# ════════════════════════════════════════════════════════════════════════════

@patch("dispatching.scheduler.estimate_job_end_time", _ninety_minutes)
class CopyYesterdayTests(_BookingFixture):
    def setUp(self):
        super().setUp()
        self.car = self.unit("6")
        self.chauffeur = self.driver("copy_driver")
        self.hold(self.car, self.chauffeur, day=PREV)

    def copy(self, **extra):
        return self.post("copy_vehicle_assignments", {"date": DAY.isoformat(), **extra})

    def test_a_clear_day_copies_with_nothing_to_say(self):
        self.job(self.chauffeur, 14)
        self.book(self.car, time(10, 0), time(12, 0), hard=True)
        self.assertEqual(self.copy(preview=True).json()["booking_notes"], [])
        body = self.copy().json()
        self.assertEqual(body["copied"], 1)
        self.assertEqual(body["skipped_booking"], [])
        self.assertEqual(body["booking_warnings"], [])
        self.assertEqual(_held(self.chauffeur), self.car.id)

    def test_a_hard_booking_is_named_in_the_preview_and_skipped_by_the_copy(self):
        self.job(self.chauffeur, 11)
        self.book(self.car, time(10, 0), time(12, 0), hard=True)
        notes = self.copy(preview=True).json()["booking_notes"]
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0]["driver_id"], self.chauffeur.id)
        self.assertTrue(notes[0]["hard"])
        self.assertIn("Tire service", notes[0]["text"])
        self.assertFalse(DriverVehicleAssignment.objects.filter(date=DAY).exists())

        resp = self.copy()
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["copied"], 0)
        self.assertEqual(len(body["skipped_booking"]), 1)
        self.assertIn("#6", body["skipped_booking"][0])
        self.assertIsNone(_held(self.chauffeur))

    def test_a_hard_skip_leaves_what_the_chauffeur_already_holds(self):
        other = self.unit("7")
        self.hold(other, self.chauffeur)                   # already on #7 today
        self.job(self.chauffeur, 11)
        self.book(self.car, time(10, 0), time(12, 0), hard=True)
        self.copy()
        self.assertEqual(_held(self.chauffeur), other.id)

    def test_a_soft_booking_is_copied_and_named(self):
        self.job(self.chauffeur, 11)
        self.book(self.car, time(10, 0), time(12, 0))
        notes = self.copy(preview=True).json()["booking_notes"]
        self.assertEqual([n["hard"] for n in notes], [False])
        body = self.copy().json()
        self.assertEqual(body["copied"], 1)
        self.assertEqual(body["skipped_booking"], [])
        self.assertEqual(len(body["booking_warnings"]), 1)
        self.assertEqual(_held(self.chauffeur), self.car.id)

    def test_an_excluded_chauffeur_is_neither_copied_nor_measured(self):
        self.job(self.chauffeur, 11)
        self.book(self.car, time(10, 0), time(12, 0), hard=True)
        body = self.copy(exclude_driver_ids=[self.chauffeur.id]).json()
        self.assertEqual(body["copied"], 0)
        self.assertEqual(body["skipped_booking"], [])

    def test_a_cancelled_booking_is_ignored(self):
        self.job(self.chauffeur, 11)
        _cancel(self.book(self.car, time(10, 0), time(12, 0), hard=True))
        self.assertEqual(self.copy(preview=True).json()["booking_notes"], [])
        body = self.copy().json()
        self.assertEqual(body["copied"], 1)
        self.assertEqual(body["skipped_booking"], [])


# ════════════════════════════════════════════════════════════════════════════
# Day Setup — Apply, and the suggester's notes
# ════════════════════════════════════════════════════════════════════════════

@patch("dispatching.scheduler.estimate_job_end_time", _ninety_minutes)
@patch.object(day_setup, "DAY_SETUP_EXCLUDE_DRIVER_IDS", set())
class DaySetupApplyTests(_BookingFixture):
    def apply(self, pairs, snapshot=None, **extra):
        return self.post("apply_day_setup", {
            "date": DAY.isoformat(),
            "pairs": [{"driver_id": d.id, "vehicle_id": v.id} for d, v in pairs],
            "snapshot": snapshot or {}, **extra})

    def test_a_hard_booking_refuses_the_batch_and_writes_nothing(self):
        car, clean_car = self.unit("6"), self.unit("7")
        booked, clean = self.driver("apply_hard"), self.driver("apply_clean")
        self.job(booked, 11)
        self.book(car, time(10, 0), time(12, 0), hard=True)
        for extra in ({}, {"override_booking": True}):
            resp = self.apply([(booked, car), (clean, clean_car)], **extra)
            self.assertEqual(resp.status_code, 409)
            body = resp.json()
            self.assertTrue(body["booking_conflict"])
            self.assertTrue(body["hard"])
            self.assertFalse(body["can_override"])
            self.assertIn("Tire service", body["error"])
            self.assertFalse(DriverVehicleAssignment.objects.filter(date=DAY).exists())

    def test_a_soft_booking_asks_then_applies_on_continue(self):
        car = self.unit("6")
        chauffeur = self.driver("apply_soft")
        self.job(chauffeur, 11)
        self.book(car, time(10, 0), time(12, 0))
        resp = self.apply([(chauffeur, car)])
        self.assertEqual(resp.status_code, 409)
        body = resp.json()
        self.assertEqual((body["booking_conflict"], body["hard"], body["can_override"]),
                         (True, False, True))
        self.assertEqual([c["driver_id"] for c in body["booking_conflicts"]], [chauffeur.id])
        self.assertIsNone(_held(chauffeur))
        resp = self.apply([(chauffeur, car)], override_booking=True)
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.json()["created"], 1)
        self.assertEqual(_held(chauffeur), car.id)

    def test_every_soft_clash_is_in_the_one_question(self):
        car_a, car_b = self.unit("6"), self.unit("7")
        a, b = self.driver("apply_a"), self.driver("apply_b")
        self.job(a, 11)
        self.job(b, 9)
        self.book(car_a, time(10, 0), time(12, 0), reason="Tire service")
        self.book(car_b, time(10, 0), time(12, 0), reason="Detail")
        body = self.apply([(a, car_a), (b, car_b)]).json()
        self.assertFalse(body["hard"])
        self.assertIn("Tire service", body["error"])
        self.assertIn("Detail", body["error"])
        self.assertEqual(len(body["booking_conflicts"]), 2)

    def test_hard_wins_over_soft_and_lists_both(self):
        car_a, car_b = self.unit("6"), self.unit("7")
        a, b = self.driver("mix_a"), self.driver("mix_b")
        self.job(a, 11)
        self.job(b, 11)
        self.book(car_a, time(10, 0), time(12, 0), hard=True, reason="Brakes")
        self.book(car_b, time(10, 0), time(12, 0), reason="Detail")
        body = self.apply([(a, car_a), (b, car_b)], override_booking=True).json()
        self.assertTrue(body["hard"])
        self.assertFalse(body["can_override"])
        self.assertIn("Brakes", body["error"])
        self.assertNotIn("Detail", body["error"])
        self.assertEqual({c["hard"] for c in body["booking_conflicts"]}, {True, False})
        self.assertFalse(DriverVehicleAssignment.objects.filter(date=DAY).exists())

    def test_a_car_the_chauffeur_already_holds_is_not_re_asked(self):
        car = self.unit("6")
        chauffeur = self.driver("apply_held")
        self.hold(car, chauffeur)
        self.job(chauffeur, 11)
        self.book(car, time(10, 0), time(12, 0), hard=True)
        resp = self.apply([(chauffeur, car)], snapshot={str(chauffeur.id): car.id})
        self.assertEqual(resp.status_code, 200, resp.content)

    def test_a_cancelled_booking_is_ignored(self):
        car = self.unit("6")
        chauffeur = self.driver("apply_cancel")
        self.job(chauffeur, 11)
        _cancel(self.book(car, time(10, 0), time(12, 0), hard=True))
        resp = self.apply([(chauffeur, car)])
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(_held(chauffeur), car.id)


@patch("dispatching.scheduler.estimate_job_end_time", _ninety_minutes)
@patch.object(day_setup, "DAY_SETUP_EXCLUDE_DRIVER_IDS", set())
class DaySetupSuggestTests(_BookingFixture):
    def test_notes_land_on_the_pairs_and_the_units(self):
        from dispatching.views import _note_day_setup_bookings
        car, free = self.unit("6"), self.unit("7")
        inside, clear = self.driver("note_inside"), self.driver("note_clear")
        self.job(inside, 11)
        self.job(clear, 14)
        self.book(car, time(10, 0), time(12, 0), hard=True)
        self.book(free, time(15, 0), time(16, 0))
        proposal = {
            "rows": [
                {"driver_id": inside.id, "vehicle_id": car.id, "group": "suggested",
                 "unit_options": [{"id": car.id}, {"id": free.id}]},
                {"driver_id": clear.id, "vehicle_id": car.id, "group": "suggested"},
                {"driver_id": clear.id, "vehicle_id": None, "group": "off"},
            ],
            "mint_proposals": [{"driver_id": inside.id, "vehicle_id": car.id}],
            "free_units": [{"id": free.id}],
        }
        _note_day_setup_bookings(proposal, DAY)
        noted = proposal["rows"][0]
        self.assertTrue(noted["booking_hard"])
        self.assertIn("Tire service", noted["booking_note"])
        self.assertNotIn("booking_note", proposal["rows"][1])     # booked, but clear of it
        self.assertTrue(proposal["mint_proposals"][0]["booking_hard"])
        self.assertEqual(noted["unit_options"][0]["booking"],
                         "Hard-booked 10a–12p Tire service")
        self.assertEqual(proposal["free_units"][0]["booking"], "Booked 3p–4p Tire service")

    def test_the_endpoint_notes_a_locked_row(self):
        car = self.unit("6")
        chauffeur = self.driver("locked_row")
        self.hold(car, chauffeur)
        self.job(chauffeur, 11)
        self.book(car, time(10, 0), time(12, 0))
        resp = self.post("suggest_day_setup", {"date": DAY.isoformat()})
        self.assertEqual(resp.status_code, 200, resp.content)
        row = next(r for r in resp.json()["rows"] if r["driver_id"] == chauffeur.id)
        self.assertFalse(row["booking_hard"])
        self.assertIn("#6", row["booking_note"])

    def test_a_day_with_no_bookings_is_untouched(self):
        from dispatching.views import _note_day_setup_bookings
        proposal = {"rows": [{"driver_id": 1, "vehicle_id": 1, "group": "suggested",
                              "unit_options": [{"id": 1}]}]}
        _note_day_setup_bookings(proposal, DAY)
        self.assertNotIn("booking", proposal["rows"][0]["unit_options"][0])


# ════════════════════════════════════════════════════════════════════════════
# The schedule board's tag
# ════════════════════════════════════════════════════════════════════════════

@patch("dispatching.scheduler.estimate_job_end_time", _ninety_minutes)
class BoardTagTests(_BookingFixture):
    def setUp(self):
        super().setUp()
        self.car = self.unit("6")
        self.chauffeur = self.driver("board_driver")
        self.hold(self.car, self.chauffeur)

    def tags(self):
        resp = self.client.get(reverse("schedule_board"), {"date": DAY.isoformat()})
        self.assertEqual(resp.status_code, 200)
        row = next(r for r in resp.context["inhouse_timeline"]
                   if r["driver"].id == self.chauffeur.id)
        return resp, row["vehicle_bookings"]

    def test_a_booking_clear_of_the_rows_trips_is_plain_information(self):
        self.job(self.chauffeur, 14)
        self.book(self.car, time(10, 0), time(12, 0))
        resp, tags = self.tags()
        self.assertEqual(len(tags), 1)
        self.assertEqual(tags[0]["short"], "Booked 10a–12p · Tire service")
        self.assertEqual(tags[0]["purpose"], "Tire service")
        self.assertFalse(tags[0]["conflict"])
        self.assertEqual(tags[0]["icon"], "calendar-event")
        self.assertContains(resp, "Booked 10a–12p · Tire service")

    def test_a_trip_inside_it_is_a_conflict(self):
        self.job(self.chauffeur, 11)
        self.book(self.car, time(10, 0), time(12, 0))
        _, tags = self.tags()
        self.assertTrue(tags[0]["conflict"])
        self.assertEqual(tags[0]["icon"], "exclamation-triangle-fill")
        self.assertIn("11:00 AM", tags[0]["conflict_text"])

    def test_a_hard_booking_carries_the_lock(self):
        self.job(self.chauffeur, 14)
        self.book(self.car, time(10, 0), time(12, 0), hard=True)
        _, tags = self.tags()
        self.assertTrue(tags[0]["hard"])
        self.assertEqual(tags[0]["icon"], "lock-fill")
        self.assertTrue(tags[0]["short"].startswith("Hard-booked 10a–12p"))

    def test_a_trip_moved_into_the_booking_turns_the_tag_into_a_conflict(self):
        leg = self.job(self.chauffeur, 14)
        self.book(self.car, time(10, 0), time(12, 0), hard=True)
        self.assertFalse(self.tags()[1][0]["conflict"])
        # A flight match / advisor retime moves the pickup after it was assigned.
        type(leg).objects.filter(id=leg.id).update(pickup_time=time(10, 30))
        _, tags = self.tags()
        self.assertTrue(tags[0]["conflict"])
        self.assertIn("10:30 AM", tags[0]["conflict_text"])

    def test_a_co_drivers_trip_does_not_mark_this_row(self):
        partner = self.driver("board_partner")
        self.hold(self.car, partner)
        self.job(partner, 11)
        self.job(self.chauffeur, 14)
        self.book(self.car, time(10, 0), time(12, 0))
        _, tags = self.tags()
        self.assertFalse(tags[0]["conflict"])

    def test_a_cancelled_booking_is_gone_from_the_board(self):
        self.job(self.chauffeur, 11)
        _cancel(self.book(self.car, time(10, 0), time(12, 0), hard=True))
        resp, tags = self.tags()
        self.assertEqual(tags, [])
        self.assertNotContains(resp, "Booked 10a–12p")


# ════════════════════════════════════════════════════════════════════════════
# Legs dashboard + planner: pool cards, assigned-car chips, dropdown labels
# ════════════════════════════════════════════════════════════════════════════

@patch("dispatching.scheduler.estimate_job_end_time", _ninety_minutes)
class DashboardTagTests(_BookingFixture):
    def setUp(self):
        super().setUp()
        self.car = self.unit("6")
        self.chauffeur = self.driver("dash_driver")
        self.hold(self.car, self.chauffeur)

    def dashboard(self):
        resp = self.client.get(reverse("dashboard"), {"date": DAY.isoformat()})
        self.assertEqual(resp.status_code, 200)
        return resp

    def chip_car(self, resp, context_key="inhouse_driver_rows"):
        row = next(r for r in resp.context[context_key]
                   if r["driver"].id == self.chauffeur.id)
        return row["assignment"].vehicle

    def test_the_assigned_car_chip_and_the_pool_card_carry_the_same_tag(self):
        self.job(self.chauffeur, 11)
        self.book(self.car, time(10, 0), time(12, 0))
        resp = self.dashboard()
        chip = self.chip_car(resp).booking_rows
        pool = next(v for v in resp.context["inhouse_vehicles"]
                    if v.id == self.car.id).booking_rows
        self.assertEqual(chip, pool)
        self.assertTrue(chip[0]["conflict"])
        self.assertEqual(chip[0]["purpose"], "Tire service")

    def test_a_booked_car_nothing_lands_on_reads_as_information(self):
        self.job(self.chauffeur, 14)
        self.book(self.car, time(10, 0), time(12, 0))
        chip = self.chip_car(self.dashboard()).booking_rows
        self.assertFalse(chip[0]["conflict"])
        self.assertEqual(chip[0]["icon"], "calendar-event")

    def test_the_trip_dropdown_names_the_booking(self):
        self.job(self.chauffeur, 14)
        self.book(self.car, time(10, 0), time(12, 0))
        resp = self.dashboard()
        label = f"{self.chauffeur} - #6 · Booked 10a–12p Tire service"
        driver = next(d for d in resp.context["inhouse_drivers_list"]
                      if d.id == self.chauffeur.id)
        self.assertEqual(driver.dashboard_display_name, label)
        # The stub <option> on the row and the cloned full list must read alike.
        self.assertContains(resp, f"{label} (1 leg)")

    def test_a_hard_booking_says_so_in_the_dropdown(self):
        self.book(self.car, time(10, 0), time(12, 0), hard=True)
        resp = self.dashboard()
        self.assertContains(resp, f"{self.chauffeur} - #6 · Hard-booked 10a–12p Tire service")

    def test_a_cancelled_booking_leaves_everything_plain(self):
        self.job(self.chauffeur, 11)
        _cancel(self.book(self.car, time(10, 0), time(12, 0), hard=True))
        resp = self.dashboard()
        self.assertEqual(self.chip_car(resp).booking_rows, [])
        self.assertTrue(all(v.booking_rows == [] for v in resp.context["inhouse_vehicles"]))
        driver = next(d for d in resp.context["inhouse_drivers_list"]
                      if d.id == self.chauffeur.id)
        self.assertEqual(driver.dashboard_display_name, f"{self.chauffeur} - #6")

    def test_the_planners_assigned_car_chip_carries_it_too(self):
        self.job(self.chauffeur, 11)
        self.book(self.car, time(10, 0), time(12, 0), hard=True)
        resp = self.client.get(reverse("capacity_planner"), {"date": DAY.isoformat()})
        self.assertEqual(resp.status_code, 200)
        chip = self.chip_car(resp, "vehicle_assign_rows").booking_rows
        self.assertEqual(len(chip), 1)
        self.assertTrue(chip[0]["hard"])
        self.assertTrue(chip[0]["conflict"])
