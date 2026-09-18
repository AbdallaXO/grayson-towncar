"""
Tests for the fleet Service board (dispatching/fleet_service.py and its page).

The board exists so the whole fleet's maintenance intervals can be read and
edited from one screen instead of one visit per vehicle page.

The properties worth guarding are the honest ones the rest of the fleet layer
already holds to: an interval with no baseline never grows an invented due
date, an unknown odometer is never rendered as zero, and a cell that is merely
empty never reads as configured.
"""
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import NoReverseMatch, reverse
from django.utils import timezone

from dispatching import fleet_service
from dispatching.mileage import METERS_PER_MILE
from drivers.models import FleetVehicle, VehicleServiceSchedule

TODAY = timezone.localdate()


def meters(miles):
    """Miles -> meters, as the odometer column stores them."""
    return (Decimal(str(miles)) * METERS_PER_MILE).quantize(Decimal("0.1"))


class ServiceBoardFixture(TestCase):
    def unit(self, number="001", odo=None, **kw):
        return FleetVehicle.objects.create(
            vehicle_number=number, year=2022, make="Chevrolet", model="Suburban",
            samsara_vehicle_id="veh-" + number,
            samsara_odometer_meters=meters(odo) if odo is not None else None,
            **kw,
        )

    def interval(self, vehicle, service_type="oil", **kw):
        return VehicleServiceSchedule.objects.create(
            vehicle=vehicle, service_type=service_type, **kw)

    def cell(self, board, unit_number, service_type):
        for row in board["rows"]:
            if row["vehicle"].vehicle_number == unit_number:
                for cell in row["cells"]:
                    if cell["service_type"] == service_type:
                        return cell
        raise AssertionError("no %s cell for unit %s" % (service_type, unit_number))


# ════════════════════════════════════════════════════════════════════════════
# Which units and which columns the board carries
# ════════════════════════════════════════════════════════════════════════════

class BoardShapeTests(ServiceBoardFixture):
    def test_every_active_unit_gets_a_row(self):
        self.unit("001")
        self.unit("002")
        board = fleet_service.load_service_board(today=TODAY)
        self.assertEqual(
            ["001", "002"], [r["vehicle"].vehicle_number for r in board["rows"]])

    def test_a_retired_unit_is_left_out(self):
        self.unit("001")
        self.unit("009", is_active=False)
        board = fleet_service.load_service_board(today=TODAY)
        self.assertEqual(["001"], [r["vehicle"].vehicle_number for r in board["rows"]])

    def test_units_sort_the_way_a_human_reads_a_unit_board(self):
        for number in ("10", "002", "001"):
            self.unit(number)
        board = fleet_service.load_service_board(today=TODAY)
        self.assertEqual(
            ["001", "002", "10"], [r["vehicle"].vehicle_number for r in board["rows"]])

    def test_the_four_everyday_services_are_always_columns(self):
        self.unit("001")
        board = fleet_service.load_service_board(today=TODAY)
        self.assertEqual(
            ["oil", "tires", "brakes", "inspection"],
            [c["service_type"] for c in board["columns"]])

    def test_a_rare_service_becomes_a_column_only_once_a_car_has_one(self):
        unit = self.unit("001")
        board = fleet_service.load_service_board(today=TODAY)
        self.assertNotIn("transmission", [c["service_type"] for c in board["columns"]])

        self.interval(unit, "transmission", interval_miles=60_000)
        board = fleet_service.load_service_board(today=TODAY)
        self.assertEqual("transmission", board["columns"][-1]["service_type"])


# ════════════════════════════════════════════════════════════════════════════
# What one cell says
# ════════════════════════════════════════════════════════════════════════════

class CellTests(ServiceBoardFixture):
    def test_a_service_never_set_up_reads_as_unset_not_as_blank(self):
        self.unit("001")
        board = fleet_service.load_service_board(today=TODAY)
        cell = self.cell(board, "001", "oil")
        self.assertEqual("unset", cell["state"])
        self.assertIsNone(cell["schedule"])

    def test_an_interval_shows_both_halves_when_it_has_both(self):
        """Compact: the grid puts four of these side by side, and "180 days"
        spelled out is what pushed the last column off the screen."""
        unit = self.unit("001")
        self.interval(unit, "oil", interval_miles=5_000, interval_days=180)
        board = fleet_service.load_service_board(today=TODAY)
        self.assertEqual(
            "5,000 mi · 180d", self.cell(board, "001", "oil")["interval_text"])

    def test_a_due_point_on_both_counts_stays_on_one_short_line(self):
        unit = self.unit("001", odo=51_000)
        self.interval(unit, "oil", interval_miles=5_000, interval_days=180,
                      last_done_odometer_miles=Decimal("50000.0"),
                      last_done_on=TODAY - timedelta(days=10))
        cell = self.cell(fleet_service.load_service_board(today=TODAY), "001", "oil")
        self.assertEqual("ok", cell["state"])
        self.assertLessEqual(len(cell["due_text"]), 24, cell["due_text"])
        self.assertIn("55,000 mi", cell["due_text"])

    def test_an_interval_with_no_baseline_never_invents_a_due_date(self):
        unit = self.unit("001", odo=60_000)
        self.interval(unit, "oil", interval_miles=5_000)
        cell = self.cell(fleet_service.load_service_board(today=TODAY), "001", "oil")
        self.assertEqual("no_baseline", cell["state"])
        self.assertEqual("no baseline", cell["due_text"])

    def test_a_healthy_interval_says_the_odometer_it_falls_due_at(self):
        unit = self.unit("001", odo=51_000)
        self.interval(unit, "oil", interval_miles=5_000,
                      last_done_odometer_miles=Decimal("50000.0"))
        cell = self.cell(fleet_service.load_service_board(today=TODAY), "001", "oil")
        self.assertEqual("ok", cell["state"])
        self.assertIn("55,000 mi", cell["due_text"])

    def test_an_interval_past_its_mileage_reads_critical(self):
        unit = self.unit("001", odo=56_240)
        self.interval(unit, "oil", interval_miles=5_000,
                      last_done_odometer_miles=Decimal("50000.0"))
        cell = self.cell(fleet_service.load_service_board(today=TODAY), "001", "oil")
        self.assertEqual("critical", cell["state"])
        self.assertIn("1,240 mi past due", cell["due_text"])

    def test_an_interval_coming_up_on_its_date_reads_warn(self):
        unit = self.unit("001")
        self.interval(unit, "inspection", interval_days=365,
                      last_done_on=TODAY - timedelta(days=360))
        cell = self.cell(
            fleet_service.load_service_board(today=TODAY), "001", "inspection")
        self.assertEqual("warn", cell["state"])
        self.assertIn("5d", cell["due_text"])

    def test_a_car_with_no_odometer_cannot_be_called_overdue_on_mileage(self):
        unit = self.unit("001")  # never reported an odometer
        self.interval(unit, "oil", interval_miles=5_000,
                      last_done_odometer_miles=Decimal("50000.0"))
        board = fleet_service.load_service_board(today=TODAY)
        row = board["rows"][0]
        self.assertIsNone(row["odometer"])
        self.assertEqual("ok", self.cell(board, "001", "oil")["state"])


# ════════════════════════════════════════════════════════════════════════════
# The strip over the grid
# ════════════════════════════════════════════════════════════════════════════

class StripTests(ServiceBoardFixture):
    def test_an_overdue_service_is_named_in_due_now(self):
        unit = self.unit("004", odo=56_240)
        self.interval(unit, "oil", interval_miles=5_000,
                      last_done_odometer_miles=Decimal("50000.0"))
        board = fleet_service.load_service_board(today=TODAY)
        self.assertEqual(1, len(board["due_now"]))
        item = board["due_now"][0]
        self.assertEqual("004", item["unit"])
        self.assertEqual("Oil change", item["label"])

    def test_a_service_coming_up_is_named_in_due_soon_not_due_now(self):
        unit = self.unit("007")
        self.interval(unit, "inspection", interval_days=365,
                      last_done_on=TODAY - timedelta(days=360))
        board = fleet_service.load_service_board(today=TODAY)
        self.assertEqual([], board["due_now"])
        self.assertEqual(["007"], [i["unit"] for i in board["due_soon"]])

    def test_the_most_overdue_car_is_named_first(self):
        far = self.unit("002", odo=60_000)
        near = self.unit("003", odo=55_100)
        for unit in (far, near):
            self.interval(unit, "oil", interval_miles=5_000,
                          last_done_odometer_miles=Decimal("50000.0"))
        board = fleet_service.load_service_board(today=TODAY)
        self.assertEqual(["002", "003"], [i["unit"] for i in board["due_now"]])

    def test_it_counts_the_cars_that_have_nothing_set_up_at_all(self):
        self.interval(self.unit("001"), "oil", interval_miles=5_000)
        self.unit("002")
        self.unit("003")
        board = fleet_service.load_service_board(today=TODAY)
        self.assertEqual(2, board["no_intervals"])

    def test_it_counts_the_intervals_that_can_never_come_due(self):
        unit = self.unit("001", odo=51_000)
        self.interval(unit, "oil", interval_miles=5_000)             # no baseline
        self.interval(unit, "tires", interval_miles=7_500,
                      last_done_odometer_miles=Decimal("50000.0"))   # fine
        board = fleet_service.load_service_board(today=TODAY)
        self.assertEqual(1, board["no_baseline"])


# ════════════════════════════════════════════════════════════════════════════
# The page
# ════════════════════════════════════════════════════════════════════════════

class ServicePageTests(ServiceBoardFixture):
    def setUp(self):
        self.staff = User.objects.create_user(
            "fleetmgr", "f@example.com", "pw", is_staff=True)
        self.client.force_login(self.staff)

    def test_the_page_renders_every_unit(self):
        self.unit("001")
        self.unit("002")
        response = self.client.get(reverse("fleet_service"))
        self.assertEqual(200, response.status_code)
        self.assertContains(response, "001")
        self.assertContains(response, "002")

    def test_an_unknown_odometer_renders_as_a_dash_never_as_zero(self):
        self.unit("001")
        response = self.client.get(reverse("fleet_service"))
        self.assertNotContains(response, ">0 mi<")
        self.assertContains(response, "&mdash;")

    def test_the_page_is_closed_to_someone_who_is_not_staff(self):
        self.client.force_login(
            User.objects.create_user("guest", "g@example.com", "pw"))
        response = self.client.get(reverse("fleet_service"))
        self.assertNotEqual(200, response.status_code)

    def test_the_service_tab_is_offered_on_the_other_fleet_pages(self):
        self.unit("001")
        response = self.client.get(reverse("fleet_desk"))
        self.assertContains(response, reverse("fleet_service"))


# ════════════════════════════════════════════════════════════════════════════
# Editing a square
# ════════════════════════════════════════════════════════════════════════════

class EditFromTheBoardTests(ServiceBoardFixture):
    def setUp(self):
        self.staff = User.objects.create_user(
            "fleetmgr", "f@example.com", "pw", is_staff=True)
        self.client.force_login(self.staff)

    def save(self, unit, **payload):
        return self.client.post(
            reverse("fleet_save_schedule", args=[unit.pk]),
            data=payload, content_type="application/json")

    def test_saving_a_square_hands_back_the_repainted_square(self):
        unit = self.unit("001", odo=56_240)
        response = self.save(unit, service_type="oil", interval_miles=5_000,
                             last_done_odometer_miles="50000")
        self.assertEqual(200, response.status_code)
        cell = response.json()["cell"]
        self.assertEqual("critical", cell["state"])
        self.assertEqual("5,000 mi", cell["interval_text"])
        self.assertEqual("1,240 mi past due", cell["due_text"])

    def test_a_square_saved_without_a_baseline_comes_back_saying_so(self):
        unit = self.unit("001", odo=56_240)
        cell = self.save(unit, service_type="oil", interval_miles=5_000).json()["cell"]
        self.assertEqual("no_baseline", cell["state"])

    def test_removing_an_interval_leaves_the_square_unset(self):
        unit = self.unit("001")
        schedule = self.interval(unit, "oil", interval_miles=5_000)
        response = self.client.post(reverse("fleet_delete_schedule", args=[schedule.pk]))
        self.assertEqual(200, response.status_code)
        cell = self.cell(fleet_service.load_service_board(today=TODAY), "001", "oil")
        self.assertEqual("unset", cell["state"])

    def test_saving_the_same_service_twice_edits_it_instead_of_duplicating(self):
        unit = self.unit("001")
        self.save(unit, service_type="oil", interval_miles=5_000)
        self.save(unit, service_type="oil", interval_miles=7_500)
        self.assertEqual(
            1, VehicleServiceSchedule.objects.filter(vehicle=unit, service_type="oil").count())
        self.assertEqual(
            7_500,
            VehicleServiceSchedule.objects.get(vehicle=unit, service_type="oil").interval_miles)


# ════════════════════════════════════════════════════════════════════════════
# The standard-set button is deliberately gone
# ════════════════════════════════════════════════════════════════════════════

class StandardIntervalsRemovedTests(ServiceBoardFixture):
    """
    "Add the standard intervals" offered to fill the whole fleet from a table of
    generic numbers. Nobody sets a fleet up that way — a car's intervals come off
    its own service history — and a grid that had quietly filled itself with
    plausible numbers would afterwards be read as fact.

    Setting an interval is now a deliberate act on the Service board, which is
    where the gaps are visible in the first place.
    """

    def setUp(self):
        self.staff = User.objects.create_user(
            "fleetmgr", "f@example.com", "pw", is_staff=True)
        self.client.force_login(self.staff)

    def test_the_fleet_wide_route_is_gone(self):
        with self.assertRaises(NoReverseMatch):
            reverse("fleet_apply_standard_intervals_all")

    def test_the_per_vehicle_route_is_gone(self):
        with self.assertRaises(NoReverseMatch):
            reverse("fleet_apply_standard_intervals", args=[1])

    def test_the_desk_points_at_the_service_board_instead_of_offering_the_button(self):
        self.unit("001")
        response = self.client.get(reverse("fleet_desk"))
        self.assertNotContains(response, "Add the intervals")
        self.assertContains(response, reverse("fleet_service"))

    def test_a_vehicle_with_nothing_set_is_still_told_so_on_its_own_page(self):
        unit = self.unit("001")
        response = self.client.get(reverse("fleet_detail", args=[unit.pk]))
        self.assertNotContains(response, "use the standard set")
        self.assertContains(response, "No service intervals set")


# ════════════════════════════════════════════════════════════════════════════
# Logging a service from the grid
# ════════════════════════════════════════════════════════════════════════════

class LogFromTheBoardTests(ServiceBoardFixture):
    """
    "Just done" — the click after the car comes back from the shop.

    It writes a real VehicleServiceRecord, not merely a new baseline, because
    the Report counts records: service cost, repair count and the
    preventative-on-time rate all come from them. A grid that only nudged
    baselines would leave the Report permanently empty.
    """

    def setUp(self):
        self.staff = User.objects.create_user(
            "fleetmgr", "f@example.com", "pw", is_staff=True)
        self.client.force_login(self.staff)

    def log(self, unit, **payload):
        return self.client.post(
            reverse("fleet_add_service", args=[unit.pk]),
            data=payload, content_type="application/json")

    def test_logging_one_resets_the_clock_and_hands_back_the_square(self):
        unit = self.unit("004", odo=56_240)
        self.interval(unit, "oil", interval_miles=5_000,
                      last_done_odometer_miles=Decimal("50000.0"))
        # Overdue before.
        self.assertEqual(
            "critical",
            self.cell(fleet_service.load_service_board(today=TODAY), "004", "oil")["state"])

        response = self.log(unit, service_type="oil", performed_on=str(TODAY),
                            odometer_miles="56240")
        self.assertEqual(200, response.status_code)

        cell = response.json()["cell"]
        self.assertEqual("ok", cell["state"])
        self.assertIn("61,240 mi", cell["due_text"])

    def test_logging_one_leaves_a_record_the_report_can_count(self):
        from drivers.models import VehicleServiceRecord

        unit = self.unit("004", odo=56_240)
        self.interval(unit, "oil", interval_miles=5_000,
                      last_done_odometer_miles=Decimal("50000.0"))
        self.log(unit, service_type="oil", performed_on=str(TODAY),
                 odometer_miles="56240", cost="89.50")

        record = VehicleServiceRecord.objects.get(vehicle=unit, service_type="oil")
        self.assertEqual(Decimal("89.50"), record.cost)
        self.assertEqual(self.staff, record.created_by)

    def test_a_receipt_from_before_the_last_service_does_not_wind_the_clock_back(self):
        unit = self.unit("004", odo=56_240)
        schedule = self.interval(unit, "oil", interval_miles=5_000,
                                 last_done_on=TODAY - timedelta(days=5),
                                 last_done_odometer_miles=Decimal("55000.0"))
        self.log(unit, service_type="oil",
                 performed_on=str(TODAY - timedelta(days=90)),
                 odometer_miles="40000")
        schedule.refresh_from_db()
        self.assertEqual(TODAY - timedelta(days=5), schedule.last_done_on)
        self.assertEqual(Decimal("55000.0"), schedule.last_done_odometer_miles)

    def test_logging_against_a_service_with_no_interval_still_records_it(self):
        """No interval to advance is not a reason to refuse the record — the
        Report still counts it. The square simply has no clock to reset."""
        from drivers.models import VehicleServiceRecord

        unit = self.unit("004", odo=56_240)
        response = self.log(unit, service_type="brakes", performed_on=str(TODAY))
        self.assertEqual(200, response.status_code)
        self.assertTrue(
            VehicleServiceRecord.objects.filter(vehicle=unit, service_type="brakes").exists())
        self.assertIsNone(response.json()["cell"])


# ════════════════════════════════════════════════════════════════════════════
# The forecast — when each service falls due at the rate the car is working
# ════════════════════════════════════════════════════════════════════════════

class ForecastTests(ServiceBoardFixture):
    """
    Projections use the same arithmetic the desk already projects from
    (``mileage.days_to_cover`` over a 30-day window), so a date here and a date
    on the desk cannot disagree.

    The refusals matter more than the numbers. A car with no baseline, no
    odometer or no recent mileage gets NO date and is counted as unprojectable
    — a forecast that quietly omitted them would read as a quiet quarter.
    """

    def miles(self, unit, per_day, days=30):
        from drivers.models import VehicleDayReading
        for n in range(1, days + 1):
            VehicleDayReading.objects.create(
                vehicle=unit, date=TODAY - timedelta(days=n),
                miles_driven=Decimal(str(per_day)))

    def test_a_mileage_interval_is_projected_from_how_hard_the_car_works(self):
        unit = self.unit("004", odo=50_000)
        self.interval(unit, "oil", interval_miles=1_000,
                      last_done_odometer_miles=Decimal("50000.0"))
        self.miles(unit, 100)          # 1,000 miles to go at 100/day -> 10 days
        forecast = fleet_service.load_forecast(today=TODAY, horizon_days=90)
        item = forecast["items"][0]
        self.assertEqual("004", item["unit"])
        self.assertEqual(TODAY + timedelta(days=10), item["date"])
        self.assertEqual("rate", item["basis"])
        self.assertEqual(Decimal("100.0"), item["per_day"])

    def test_a_date_interval_is_an_exact_date_not_an_estimate(self):
        unit = self.unit("002")
        self.interval(unit, "inspection", interval_days=365,
                      last_done_on=TODAY - timedelta(days=335))
        forecast = fleet_service.load_forecast(today=TODAY, horizon_days=90)
        item = forecast["items"][0]
        self.assertEqual(TODAY + timedelta(days=30), item["date"])
        self.assertEqual("date", item["basis"])
        self.assertIsNone(item["per_day"])

    def test_whichever_comes_first_wins_when_an_interval_has_both(self):
        unit = self.unit("005", odo=50_000)
        self.interval(unit, "oil", interval_miles=1_000, interval_days=365,
                      last_done_odometer_miles=Decimal("50000.0"),
                      last_done_on=TODAY)
        self.miles(unit, 100)          # miles run out in 10 days, the date in 365
        item = fleet_service.load_forecast(today=TODAY, horizon_days=90)["items"][0]
        self.assertEqual(TODAY + timedelta(days=10), item["date"])
        self.assertEqual("rate", item["basis"])

    def test_a_parked_car_is_refused_a_date_rather_than_given_a_far_one(self):
        unit = self.unit("006", odo=50_000)
        self.interval(unit, "oil", interval_miles=1_000,
                      last_done_odometer_miles=Decimal("50000.0"))
        self.miles(unit, 0)            # provably not moving
        forecast = fleet_service.load_forecast(today=TODAY, horizon_days=90)
        self.assertEqual([], forecast["items"])
        self.assertEqual(1, forecast["unprojectable"]["not_moving"])

    def test_an_interval_with_no_baseline_is_counted_not_hidden(self):
        unit = self.unit("007", odo=50_000)
        self.interval(unit, "oil", interval_miles=1_000)
        self.miles(unit, 100)
        forecast = fleet_service.load_forecast(today=TODAY, horizon_days=90)
        self.assertEqual([], forecast["items"])
        self.assertEqual(1, forecast["unprojectable"]["no_baseline"])

    def test_a_car_with_no_recent_mileage_at_all_is_counted(self):
        unit = self.unit("008", odo=50_000)
        self.interval(unit, "oil", interval_miles=1_000,
                      last_done_odometer_miles=Decimal("50000.0"))
        forecast = fleet_service.load_forecast(today=TODAY, horizon_days=90)
        self.assertEqual([], forecast["items"])
        self.assertEqual(1, forecast["unprojectable"]["no_rate"])

    def test_something_already_overdue_leads_the_list(self):
        late = self.unit("009", odo=56_000)
        self.interval(late, "oil", interval_miles=1_000,
                      last_done_odometer_miles=Decimal("50000.0"))
        self.miles(late, 100)
        soon = self.unit("010", odo=50_000)
        self.interval(soon, "oil", interval_miles=1_000,
                      last_done_odometer_miles=Decimal("50000.0"))
        self.miles(soon, 100)

        forecast = fleet_service.load_forecast(today=TODAY, horizon_days=90)
        self.assertEqual(["009", "010"], [i["unit"] for i in forecast["items"]])
        self.assertEqual("Overdue", forecast["items"][0]["bucket"])

    def test_the_horizon_leaves_out_what_is_further_off(self):
        unit = self.unit("004", odo=50_000)
        self.interval(unit, "oil", interval_miles=6_000,
                      last_done_odometer_miles=Decimal("50000.0"))
        self.miles(unit, 100)          # 60 days out
        self.assertEqual(
            [], fleet_service.load_forecast(today=TODAY, horizon_days=30)["items"])
        self.assertEqual(
            1, len(fleet_service.load_forecast(today=TODAY, horizon_days=90)["items"]))

    def test_the_blind_spots_are_phrased_as_a_clean_sentence(self):
        """Built in Python, not stitched together by template tags — the tag
        version left a space before the full stop."""
        self.interval(self.unit("001", odo=50_000), "oil", interval_miles=5_000)
        blind = fleet_service.load_forecast(today=TODAY, horizon_days=90)["blind_spots"]
        self.assertEqual(["1 with no last service to count from"], blind)

    def test_several_kinds_of_blind_spot_are_named_separately(self):
        self.interval(self.unit("001", odo=50_000), "oil", interval_miles=5_000)
        self.interval(self.unit("002", odo=50_000), "tires", interval_miles=7_500,
                      last_done_odometer_miles=Decimal("50000.0"))
        blind = fleet_service.load_forecast(today=TODAY, horizon_days=90)["blind_spots"]
        self.assertEqual(
            ["1 with no last service to count from", "1 with no recent mileage"], blind)

    def test_the_near_ones_are_grouped_by_week_and_the_rest_by_month(self):
        unit = self.unit("004", odo=50_000)
        self.interval(unit, "oil", interval_miles=300,
                      last_done_odometer_miles=Decimal("50000.0"))
        self.miles(unit, 100)          # 3 days out
        item = fleet_service.load_forecast(today=TODAY, horizon_days=90)["items"][0]
        self.assertEqual("This week", item["bucket"])


class ForecastPageTests(ServiceBoardFixture):
    def setUp(self):
        self.staff = User.objects.create_user(
            "fleetmgr", "f@example.com", "pw", is_staff=True)
        self.client.force_login(self.staff)

    def test_the_service_page_offers_the_forecast_view(self):
        self.unit("001")
        response = self.client.get(reverse("fleet_service"))
        self.assertContains(response, "Forecast")

    def test_the_forecast_view_renders(self):
        self.unit("001")
        response = self.client.get(reverse("fleet_service"), {"view": "forecast"})
        self.assertEqual(200, response.status_code)

    def test_it_says_out_loud_what_it_cannot_see(self):
        """An empty forecast that really means "we do not know" is the one way
        this page could do harm, so the blind spots are always stated."""
        self.interval(self.unit("001", odo=50_000), "oil", interval_miles=5_000)
        response = self.client.get(reverse("fleet_service"), {"view": "forecast"})
        self.assertContains(response, "cannot be projected")
        self.assertContains(response, "no last service to count from")

    def test_a_fleet_it_can_see_all_of_says_that_instead(self):
        from drivers.models import VehicleDayReading

        unit = self.unit("001", odo=50_000)
        self.interval(unit, "oil", interval_miles=1_000,
                      last_done_odometer_miles=Decimal("50000.0"))
        for n in range(1, 31):
            VehicleDayReading.objects.create(
                vehicle=unit, date=TODAY - timedelta(days=n), miles_driven=Decimal("100"))

        response = self.client.get(reverse("fleet_service"), {"view": "forecast"})
        self.assertContains(response, "Nothing is invisible")
        self.assertNotContains(response, "cannot be projected")

    def test_an_estimate_is_never_printed_as_a_certainty(self):
        """A projected date carries the rate it rests on; a calendar deadline
        says it is one. Someone books a shop day off this page."""
        from drivers.models import VehicleDayReading

        unit = self.unit("004", odo=50_000)
        self.interval(unit, "oil", interval_miles=1_000,
                      last_done_odometer_miles=Decimal("50000.0"))
        for n in range(1, 31):
            VehicleDayReading.objects.create(
                vehicle=unit, date=TODAY - timedelta(days=n), miles_driven=Decimal("100"))
        self.interval(self.unit("002"), "inspection", interval_days=365,
                      last_done_on=TODAY - timedelta(days=340))

        response = self.client.get(
            reverse("fleet_service"), {"view": "forecast", "days": 90})
        self.assertContains(response, "mi/day")
        self.assertContains(response, "calendar date")

    def test_an_unknown_horizon_falls_back_instead_of_erroring(self):
        self.unit("001")
        response = self.client.get(
            reverse("fleet_service"), {"view": "forecast", "days": "banana"})
        self.assertEqual(200, response.status_code)
