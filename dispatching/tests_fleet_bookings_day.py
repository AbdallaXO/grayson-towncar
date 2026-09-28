"""Vehicle bookings on Fleet → The day and the fleet-side pages: the audit's
fixes to the booking calendar itself.

Run with:  ENABLE_DEBUG_TOOLBAR=0 ./manage.py test dispatching.tests_fleet_bookings_day

Trip ends are pinned to pickup + 90 minutes (the tests_fleet_day trick), so a
clash is arithmetic: a 9:00 trip occupies 9:00–10:30.
"""
import json
from datetime import datetime, time, timedelta
from unittest.mock import patch

from django.urls import reverse

from dispatching import fleet_bookings, fleet_day, fleet_inspection
from dispatching.tests_fleet_bookings import _BookingFixture
from dispatching.tests_fleet_day import _ninety_minutes
from dispatching.tests_fleet_desk import DAY, TODAY
from drivers.models import VehicleBooking, VehicleDowntime
from reservations.models import LegStop

SHIFT = (time(7, 30), time(16, 0))


def _at(hour, minute=0, day=DAY):
    return datetime.combine(day, time(hour, minute))


# ════════════════════════════════════════════════════════════════════════════
# The strip's clock in the page script
# ════════════════════════════════════════════════════════════════════════════

@patch("dispatching.scheduler.estimate_job_end_time", _ninety_minutes)
class AxisScriptTests(_BookingFixture):
    def page(self, day=DAY):
        return self.client.get(reverse("fleet_day"), {"date": day.isoformat()})

    def test_an_axis_starting_at_midnight_is_zero_not_six(self):
        """|default read 0 as missing and every click landed six hours off."""
        unit = self.unit("6")
        driver = self.driver("midnight_holder")
        self.hold(unit, driver)
        leg = self.job(driver, 0)
        leg.pickup_time = time(0, 30)
        leg.save(update_fields=["pickup_time"])
        self.job(driver, 14)
        resp = self.page()
        self.assertEqual(resp.context["payload"]["axis_start_min"], 0)
        self.assertContains(resp, "start: 0,")
        self.assertNotContains(resp, "start: 360,")

    def test_an_unbuilt_day_still_renders_valid_numbers(self):
        """No payload at all: the script still needs a number, not 'start: ,'."""
        self.unit("6")
        self.leg(DAY, hour=9)
        resp = self.page()
        self.assertIsNone(resp.context["payload"])
        self.assertContains(resp, "start: 360,")
        self.assertContains(resp, "end: 1200")
        self.assertContains(resp, "var NOW_MIN = null;")
        self.assertNotContains(resp, "start: ,")
        self.assertNotContains(resp, "end: \n")

    def test_the_readers_hours_reach_the_script(self):
        unit = self.unit("6")
        driver = self.driver("hours_holder")
        self.hold(unit, driver)
        self.job(driver, 9)
        resp = self.page()
        payload = resp.context["payload"]
        self.assertContains(resp, f"var SHIFT_START = {payload['shift_start_min']};")

    def test_a_late_drop_marks_where_the_next_day_begins(self):
        """The strip runs past midnight for a 23:00 pickup; that stretch is
        shaded and not bookable."""
        unit = self.unit("6")
        driver = self.driver("late_holder")
        self.hold(unit, driver)
        self.job(driver, 9)
        self.job(driver, 23)                     # ends 00:30 the next day
        payload = self.payload()
        self.assertGreater(payload["axis_end_min"], 24 * 60)
        self.assertIsNotNone(payload["midnight_pct"])
        self.assertLess(payload["midnight_pct"], 100)
        self.assertContains(self.page(), 'class="fd-nextday"')

    def test_a_day_ending_before_midnight_has_no_next_day_shade(self):
        unit = self.unit("6")
        driver = self.driver("early_holder")
        self.hold(unit, driver)
        self.job(driver, 9)
        self.assertIsNone(self.payload()["midnight_pct"])
        self.assertNotContains(self.page(), 'class="fd-nextday"')


# ════════════════════════════════════════════════════════════════════════════
# A booking cuts a hole; it does not erase it
# ════════════════════════════════════════════════════════════════════════════

class UncoveredTests(_BookingFixture):
    def test_a_booking_in_the_middle_leaves_both_ends(self):
        self.assertEqual(
            fleet_day.uncovered(_at(9, 30), _at(14), [(_at(10), _at(11))]),
            [(_at(9, 30), _at(10)), (_at(11), _at(14))])

    def test_a_booking_covering_the_hole_leaves_nothing(self):
        self.assertEqual(fleet_day.uncovered(_at(10), _at(11), [(_at(9), _at(12))]), [])

    def test_touching_ends_leave_the_hole_whole(self):
        self.assertEqual(fleet_day.uncovered(_at(10), _at(12), [(_at(12), _at(13))]),
                         [(_at(10), _at(12))])

    def test_two_bookings_leave_three_pieces(self):
        self.assertEqual(
            fleet_day.uncovered(_at(8), _at(16), [(_at(13), _at(14)), (_at(9), _at(10))]),
            [(_at(8), _at(9)), (_at(10), _at(13)), (_at(14), _at(16))])


@patch("dispatching.scheduler.estimate_job_end_time", _ninety_minutes)
class SplitHoleTests(_BookingFixture):
    def setUp(self):
        super().setUp()
        self.car = self.unit("6")
        self.chauffeur = self.driver("split_holder")
        self.hold(self.car, self.chauffeur)
        self.job(self.chauffeur, 8)          # 8:00–9:30
        self.job(self.chauffeur, 14)         # 9:30 → 14:00 is the hole

    def row(self):
        return self.row_for(fleet_day.build_day(fleet_day.load_car_day(DAY), shift=SHIFT), "6")

    def test_the_founders_example_books_ten_to_twelve_and_one_is_still_free(self):
        """#006 free 10–1, booked 10–12: 12–1 keeps its own mark."""
        self.book(self.car, time(9, 30), time(12, 0))
        row = self.row()
        free = [(g["from_label"], g["to_label"]) for g in row["gaps"] if g["reachable"]]
        self.assertEqual(free, [("12:00 PM", "2:00 PM")])

    def test_a_short_leftover_is_kept_but_not_marked(self):
        """9:30–10:00 is a real piece of the day, too short to walk a car in."""
        self.book(self.car, time(10, 0), time(13, 30))
        row = self.row()
        pieces = [(g["from_label"], g["to_label"], bool(g["reachable"])) for g in row["gaps"]]
        self.assertIn(("9:30 AM", "10:00 AM", False), pieces)
        self.assertIn(("1:30 PM", "2:00 PM", False), pieces)

    def test_each_piece_has_its_own_place_on_the_strip(self):
        self.book(self.car, time(10, 0), time(11, 0))
        row = self.row()
        lefts = [g["left"] for g in row["gaps"]]
        self.assertEqual(lefts, sorted(lefts))
        self.assertEqual(len(set(lefts)), len(lefts))
        self.assertTrue(all(g.get("cut") for g in row["gaps"] if g["start"] >= _at(9, 30)))

    def test_the_phone_list_keeps_the_free_line_for_the_remainder(self):
        """Phone width has no strip: the remainder's free line is the only
        way to see it."""
        self.book(self.car, time(9, 30), time(10, 0))
        resp = self.client.get(reverse("fleet_day"), {"date": DAY.isoformat()})
        self.assertContains(resp, "free — room for shop work")

    def test_the_inspection_round_never_offers_time_inside_a_booking(self):
        self.book(self.car, time(10, 0), time(11, 0))
        window = fleet_inspection.walkable_window(self.row(), DAY, SHIFT)
        # 11:00–2:00 is what the booking leaves; the walker gets the part of it
        # the car is AT BASE — 35 min in from wherever the booking was, 12 out
        # to the MCO pickup.
        self.assertEqual((window["from_label"], window["to_label"]), ("11:35 AM", "1:48 PM"))

    def test_a_booking_over_the_whole_hole_leaves_nothing_to_walk(self):
        self.book(self.car, time(9, 30), time(14, 0))
        self.assertIsNone(fleet_inspection.walkable_window(self.row(), DAY, SHIFT))

    def test_the_strip_and_the_round_still_agree(self):
        self.book(self.car, time(10, 0), time(11, 0))
        row = self.row()
        best = max((g for g in row["gaps"] if g["reachable"]),
                   key=lambda g: g["reachable"]["minutes"])["reachable"]
        window = fleet_inspection.walkable_window(row, DAY, SHIFT)
        self.assertEqual((window["from_label"], window["to_label"]),
                         (best["from_label"], best["to_label"]))


# ════════════════════════════════════════════════════════════════════════════
# A car with no trips and a booking
# ════════════════════════════════════════════════════════════════════════════

@patch("dispatching.scheduler.estimate_job_end_time", _ninety_minutes)
class TriplessBookedCarTests(_BookingFixture):
    def setUp(self):
        super().setUp()
        # Another car working keeps the day built and confident.
        worker = self.driver("booked_day_worker")
        self.hold(self.unit("1"), worker)
        self.job(worker, 9)
        self.car = self.unit("6")

    def test_it_reads_its_booking_not_free_all_day(self):
        self.book(self.car, time(10, 0), time(12, 0))
        payload = self.payload()
        row = self.row_for(payload, "6")
        self.assertEqual(row["state"], "booked")
        self.assertNotIn("free all day", row["note"])
        self.assertIn("booked 10:00 AM–12:00 PM for Tire service", row["note"])
        self.assertEqual(payload["open_units"], 0)

    def test_a_hard_one_says_hard_booked(self):
        self.book(self.car, time(10, 0), time(12, 0), hard=True)
        self.assertIn("hard-booked 10:00 AM–12:00 PM",
                      self.row_for(self.payload(), "6")["note"])

    def test_a_held_car_with_no_trips_says_no_trips_not_nothing_booked(self):
        holder = self.driver("idle_holder")
        self.hold(self.car, holder)
        row = self.row_for(self.payload(), "6")
        self.assertEqual(row["state"], "open")
        self.assertIn("no trips on it", row["note"])
        self.assertNotIn("booked", row["note"])

    def test_the_phone_list_does_not_contradict_the_booking(self):
        self.book(self.car, time(10, 0), time(12, 0))
        resp = self.client.get(reverse("fleet_day"), {"date": DAY.isoformat()})
        self.assertNotContains(resp, "free all day")
        self.assertContains(resp, "No chauffeur on it.")

    def test_the_round_subtracts_the_booking_from_the_shift(self):
        self.book(self.car, time(8, 0), time(12, 0))
        row = self.row_for(fleet_day.build_day(fleet_day.load_car_day(DAY), shift=SHIFT), "6")
        window = fleet_inspection.walkable_window(row, DAY, SHIFT)
        self.assertEqual((window["from_label"], window["to_label"]), ("12:00 PM", "4:00 PM"))

    def test_a_car_booked_all_shift_is_not_walkable_and_not_sitting_still(self):
        self.book(self.car, time(7, 0), time(17, 0), booking_type="detailing", reason="")
        row = self.row_for(fleet_day.build_day(fleet_day.load_car_day(DAY), shift=SHIFT), "6")
        self.assertIsNone(fleet_inspection.walkable_window(row, DAY, SHIFT))
        loaded = fleet_inspection.load_week(DAY)
        week = fleet_inspection.build_week(loaded, day_rows=[row], shift=SHIFT)
        tile = next(t for t in week["tiles"] if t["number"] == "6")
        self.assertFalse(tile["idle_today"])
        self.assertTrue(tile["off_shift"])
        self.assertEqual(tile["today"], "Booked by fleet — no gap before 4:00 PM")


# ════════════════════════════════════════════════════════════════════════════
# The conflict, said on the booking
# ════════════════════════════════════════════════════════════════════════════

@patch("dispatching.scheduler.estimate_job_end_time", _ninety_minutes)
class ConflictFlagTests(_BookingFixture):
    def setUp(self):
        super().setUp()
        self.car = self.unit("6")
        self.chauffeur = self.driver("miguel")
        self.hold(self.car, self.chauffeur)
        self.job(self.chauffeur, 9)                      # 9:00–10:30

    def test_the_wording_names_the_trip_the_chauffeur_and_the_booking(self):
        self.book(self.car, time(10, 0), time(12, 0))
        booking = self.row_for(self.payload(), "6")["bookings"][0]
        self.assertEqual(booking["conflict_line"],
                         "Booking conflict — the 9:00 AM trip (Miguel) was put on "
                         "this car during Tire service.")

    def test_a_conflicting_booking_carries_a_flag_above_the_trips(self):
        booking = self.book(self.car, time(10, 0), time(12, 0))
        resp = self.client.get(reverse("fleet_day"), {"date": DAY.isoformat()})
        self.assertContains(resp, 'class="fd-book-flag"', count=1)
        self.assertContains(resp, f'data-pop="flag" data-booking="{booking.id}"')

    def test_a_tiny_conflicting_booking_still_gets_its_flag(self):
        self.book(self.car, time(10, 0), time(10, 15))
        resp = self.client.get(reverse("fleet_day"), {"date": DAY.isoformat()})
        self.assertContains(resp, "tiny")
        self.assertContains(resp, 'class="fd-book-flag"', count=1)

    def test_a_clear_booking_has_no_flag(self):
        self.book(self.car, time(13, 0), time(14, 0))
        resp = self.client.get(reverse("fleet_day"), {"date": DAY.isoformat()})
        self.assertNotContains(resp, 'class="fd-book-flag"')

    def test_bookings_are_buttons_a_keyboard_can_reach(self):
        booking = self.book(self.car, time(13, 0), time(14, 0))
        resp = self.client.get(reverse("fleet_day"), {"date": DAY.isoformat()})
        html = resp.content.decode()
        self.assertIn('<button type="button" class="fd-book', html)
        self.assertIn('<button type="button" class="fd-trip booking', html)
        self.assertIn('aria-label="#6 booked 1:00 PM–2:00 PM for Tire service"', html)
        self.assertIn(f'data-booking="{booking.id}"', html)

    def test_a_charter_is_drawn_by_its_booked_hours(self):
        """A four-hour charter at 1:00 holds the car to 5:00, so a 3:00
        booking is a conflict on The day — the same measure dispatch uses."""
        leg = self.job(self.chauffeur, 13)
        LegStop.objects.create(leg=leg, sequence=0, stop_type="charter",
                               duration_minutes=240)
        self.book(self.car, time(15, 0), time(16, 0))
        row = self.row_for(self.payload(), "6")
        charter = next(j for j in row["jobs"] if j["start"] == _at(13))
        self.assertEqual(charter["end"], _at(17))
        self.assertTrue(row["bookings"][0]["conflict"])


# ════════════════════════════════════════════════════════════════════════════
# The week: conflicts beyond the day on screen, and dates beyond the week
# ════════════════════════════════════════════════════════════════════════════

@patch("dispatching.scheduler.estimate_job_end_time", _ninety_minutes)
class WeekTests(_BookingFixture):
    def clash_on(self, day, number="6", name="week_holder"):
        car = self.unit(number)
        chauffeur = self.driver(name)
        self.hold(car, chauffeur, day=day)
        self.job(chauffeur, 9, day=day)
        return self.book(car, time(10, 0), time(12, 0), day=day)

    def test_conflicts_between_finds_a_clash_on_another_day(self):
        booking = self.clash_on(DAY)
        found = fleet_bookings.conflicts_between(TODAY, TODAY + timedelta(days=6))
        self.assertEqual([c["id"] for c in found], [booking.id])
        self.assertEqual(found[0]["date"], DAY)
        self.assertIn("the 9:00 AM trip", found[0]["text"])
        self.assertEqual(fleet_bookings.conflict_counts(found), {DAY: 1})

    def test_a_booking_nobody_holds_the_car_for_is_not_a_conflict(self):
        car = self.unit("6")
        self.book(car, time(10, 0), time(12, 0))
        self.leg(DAY, hour=10)
        self.assertEqual(fleet_bookings.conflicts_between(TODAY, DAY), [])

    def test_the_day_chips_count_the_clash(self):
        self.clash_on(DAY)
        resp = self.client.get(reverse("fleet_day"))            # today's page
        chip = next(o for o in resp.context["day_options"] if o["date"] == DAY)
        self.assertEqual(chip["conflicts"], 1)
        self.assertContains(resp, "1 booking conflict")

    def test_a_date_beyond_the_week_is_built_and_judged(self):
        """Saving a booking two weeks out lands the user there. It used to read
        'Nothing booked' with its clash never judged."""
        far = TODAY + timedelta(days=10)
        self.clash_on(far)
        resp = self.client.get(reverse("fleet_day"), {"date": far.isoformat()})
        self.assertIsNotNone(resp.context["payload"])
        self.assertContains(resp, "Booking conflict")
        self.assertNotContains(resp, "Nothing booked")


# ════════════════════════════════════════════════════════════════════════════
# The fleet desk band
# ════════════════════════════════════════════════════════════════════════════

@patch("dispatching.scheduler.estimate_job_end_time", _ninety_minutes)
class DeskBandTests(_BookingFixture):
    def test_the_desk_lists_this_weeks_booking_conflicts(self):
        car = self.unit("6")
        chauffeur = self.driver("desk_holder")
        self.hold(car, chauffeur)
        self.job(chauffeur, 9)
        self.book(car, time(10, 0), time(12, 0))
        resp = self.client.get(reverse("fleet_desk"))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.context["booking_conflicts"]), 1)
        self.assertContains(resp, "Booking conflicts")
        self.assertContains(resp, f'{reverse("fleet_day")}?date={DAY.isoformat()}')
        self.assertContains(resp, "was put on this car during Tire service")

    def test_no_conflicts_no_band(self):
        self.unit("6")
        resp = self.client.get(reverse("fleet_desk"))
        self.assertEqual(resp.context["booking_conflicts"], [])
        self.assertNotContains(resp, "Booking conflicts")

    def test_shop_days_are_not_called_bookings(self):
        """'Booking' means fleet's part-day claim from The day. A whole shop
        day off the downtime ledger is a shop day."""
        car = self.unit("6")
        VehicleDowntime.objects.create(
            vehicle=car, category="maintenance", reason="Oil change",
            starts_on=DAY, expected_back_on=DAY + timedelta(days=1))
        resp = self.client.get(reverse("fleet_desk"))
        self.assertContains(resp, "Shop days booked")
        self.assertContains(resp, "Cancel this shop day")
        self.assertNotContains(resp, "Booked in</div>")
        self.assertNotContains(resp, "Cancel this booking")


# ════════════════════════════════════════════════════════════════════════════
# Saving: the past, the stale, the notes-only edit, and bad input
# ════════════════════════════════════════════════════════════════════════════

@patch("dispatching.scheduler.estimate_job_end_time", _ninety_minutes)
class PastBookingTests(_BookingFixture):
    def setUp(self):
        super().setUp()
        self.car = self.unit("6")
        self.past = self.book(self.car, time(10, 0), time(12, 0),
                              day=TODAY - timedelta(days=1))

    def test_it_cannot_be_saved_unchanged(self):
        resp = self.save(id=self.past.id, date=self.past.date.isoformat())
        self.assertEqual(resp.status_code, 400)
        self.assertIn("kept as a record", resp.json()["error"])

    def test_it_cannot_be_moved_into_the_future(self):
        """The save check used to test only the NEW date, so yesterday's
        record could be rewritten into next week's plan."""
        resp = self.save(id=self.past.id, date=(TODAY + timedelta(days=3)).isoformat())
        self.assertEqual(resp.status_code, 400)
        self.past.refresh_from_db()
        self.assertEqual(self.past.date, TODAY - timedelta(days=1))

    def test_it_cannot_be_cancelled(self):
        resp = self.post("fleet_cancel_booking", {}, args=[self.past.id])
        self.assertEqual(resp.status_code, 400)
        self.past.refresh_from_db()
        self.assertIsNone(self.past.cancelled_at)

    def test_the_page_opens_it_read_only(self):
        resp = self.client.get(reverse("fleet_day"),
                               {"date": self.past.date.isoformat()})
        self.assertFalse(resp.context["booking_index"][self.past.id]["can_edit"])
        self.assertFalse(resp.context["can_book"])


@patch("dispatching.scheduler.estimate_job_end_time", _ninety_minutes)
class EditAckTests(_BookingFixture):
    def setUp(self):
        super().setUp()
        self.car = self.unit("6")
        self.chauffeur = self.driver("ack_edit_holder")
        self.hold(self.car, self.chauffeur)
        self.job(self.chauffeur, 9)                          # 9:00–10:30
        resp = self.save(vehicle_id=self.car.id, acknowledge=True)   # 10–12 over it
        self.assertEqual(resp.status_code, 200, resp.content)
        self.booking = VehicleBooking.objects.get()

    def test_a_notes_only_edit_saves_without_asking_again(self):
        resp = self.save(id=self.booking.id, notes="Call ahead")
        self.assertEqual(resp.status_code, 200, resp.content)
        self.booking.refresh_from_db()
        self.assertEqual(self.booking.notes, "Call ahead")

    def test_moving_it_over_a_new_trip_asks_about_that_trip(self):
        self.job(self.chauffeur, 12)                         # 12:00–13:30
        resp = self.save(id=self.booking.id, end="13:00")
        self.assertEqual(resp.status_code, 409)
        body = resp.json()
        self.assertTrue(body["needs_ack"])
        self.assertIn("12:00 PM", body["summary"])
        self.assertNotIn("9:00 AM", body["summary"])
        self.assertIn("Save it anyway?", body["summary"])

    def test_moving_it_within_the_same_clash_does_not_ask(self):
        resp = self.save(id=self.booking.id, start="10:15", end="11:45")
        self.assertEqual(resp.status_code, 200, resp.content)

    def test_making_it_hard_is_a_new_decision(self):
        self.client.force_login(self.fleet_manager())
        resp = self.save(id=self.booking.id, is_hard=True)
        self.assertEqual(resp.status_code, 409)
        self.assertIn("9:00 AM", resp.json()["summary"])

    def test_a_new_booking_still_asks_book_it_anyway(self):
        other = self.unit("7")
        holder = self.driver("second_holder")
        self.hold(other, holder)
        self.job(holder, 9)
        resp = self.save(vehicle_id=other.id)
        self.assertEqual(resp.status_code, 409)
        self.assertIn("Book it anyway?", resp.json()["summary"])


class StaleAndBadInputTests(_BookingFixture):
    def setUp(self):
        super().setUp()
        self.car = self.unit("6")

    def raw(self, body):
        return self.client.post(reverse("fleet_save_booking"), data=body,
                                content_type="application/json")

    def assert_json(self, resp, status):
        self.assertEqual(resp.status_code, status, resp.content)
        self.assertEqual(resp["Content-Type"], "application/json")
        self.assertFalse(resp.json()["success"])
        return resp.json()

    def test_editing_a_booking_someone_cancelled_is_a_json_409(self):
        booking = self.book(self.car, time(10, 0), time(12, 0))
        booking.cancelled_at = booking.created_at
        booking.save()
        body = self.assert_json(self.save(id=booking.id), 409)
        self.assertIn("reload the page", body["error"])

    def test_cancelling_twice_is_a_json_409(self):
        booking = self.book(self.car, time(10, 0), time(12, 0))
        self.assertEqual(self.post("fleet_cancel_booking", {}, args=[booking.id]).status_code, 200)
        self.assert_json(self.post("fleet_cancel_booking", {}, args=[booking.id]), 409)

    def test_a_non_numeric_car_is_a_400(self):
        self.assert_json(self.save(vehicle_id="abc"), 400)

    def test_a_non_numeric_booking_id_is_a_400(self):
        self.assert_json(self.save(id="xyz", vehicle_id=self.car.id), 400)

    def test_a_body_that_is_not_an_object_is_a_400(self):
        self.assert_json(self.raw("[1, 2]"), 400)
        self.assert_json(self.raw("42"), 400)

    def test_an_impossible_date_is_a_400(self):
        body = self.assert_json(self.save(vehicle_id=self.car.id, date="2026-02-30"), 400)
        self.assertEqual(body["error"], "Pick a date.")

    def test_a_missing_car_is_a_400(self):
        self.assert_json(self.save(), 400)
        self.assert_json(self.save(vehicle_id=999999), 400)

    def test_a_retired_car_cannot_be_booked(self):
        retired = self.unit("99", is_active=False)
        self.assert_json(self.save(vehicle_id=retired.id), 400)
        self.assertFalse(VehicleBooking.objects.exists())

    def test_the_text_false_does_not_make_a_hard_booking(self):
        resp = self.save(vehicle_id=self.car.id, is_hard="false")
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertFalse(VehicleBooking.objects.get().is_hard)


# ════════════════════════════════════════════════════════════════════════════
# Words
# ════════════════════════════════════════════════════════════════════════════

class WordingTests(_BookingFixture):
    def test_reserved_reads_as_the_founder_wrote_it(self):
        self.assertIn(("reserved", "Reserved for another operational reason"),
                      VehicleBooking.TYPE_CHOICES)

    def test_the_sheet_says_what_soft_and_hard_mean(self):
        self.unit("6")
        resp = self.client.get(reverse("fleet_day"))
        self.assertContains(resp, "The car can still be used. Anyone putting a trip on it "
                                  "in this window is shown the booking and asked first.")
        self.assertContains(resp, "No trip can go on this car in this window, and "
                                  "dispatch can't override it.")

    def test_book_a_car_opens_on_no_car(self):
        """It used to keep whichever car the last sheet showed."""
        self.unit("6")
        resp = self.client.get(reverse("fleet_day"))
        self.assertContains(resp, '<option value="" disabled>Choose a car</option>')

    def test_an_empty_day_says_no_trips_not_nothing_booked(self):
        self.unit("6")
        resp = self.client.get(reverse("fleet_day"), {"date": DAY.isoformat()})
        self.assertNotContains(resp, "Nothing booked")
        self.assertContains(resp, "No trips on")

