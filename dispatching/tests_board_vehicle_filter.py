"""Schedule board — filter the Unassigned row by vehicle type (``?vehicle=a,b``).

Run with:  ./manage.py test dispatching.tests_board_vehicle_filter

The hiding itself happens in the browser (every backlog chip carries
data-vehicle and the script toggles a class on chips in the Unassigned row
only), so what the server must guarantee is:

  * HONEST OPTIONS: the checklist offers exactly the vehicle types that have an
    UNASSIGNED job on this board today, with counts, in the fleet's own order —
    plus any type that is ticked but has no backlog job today, so it can be
    unticked. Assigned jobs never count: driver lanes are not filtered.
  * THE BOARD KEEPS ITS SHAPE: the filter changes nothing server-side — same
    rows, same backlog, same header counts, with or without it.
  * STICKY: the choice rides along on the date arrows and the board switch;
    junk in the URL is dropped quietly.
  * The control renders with the chosen types ticked.
"""
from datetime import time
from decimal import Decimal

from django.urls import reverse

from dispatching.tests_board_driver_filter import DAY, _BoardFilterFixture
from rates.models import Rate, Vehicle
from reservations.models import Leg, Reservation


class VehicleFilterTests(_BoardFilterFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.van = Vehicle.objects.create(
            vehicle_type="van", capacity=10, luggage_capacity=10)
        cls.van_rate = Rate.objects.create(
            vehicle=cls.van, route=cls.route,
            oneway_price=Decimal("200.00"), round_trip_price=Decimal("360.00"))

    def _van_leg(self, pickup_time=time(9, 0), driver=None):
        res = Reservation.objects.create(
            trip_type="one-way", customer=self.customer, rate=self.van_rate,
            vehicle=self.van, base_price=Decimal("200.00"),
            total_price=Decimal("200.00"))
        return Leg.objects.create(
            reservation=res, pickup_date=DAY, pickup_time=pickup_time,
            pickup_location="MCO", dropoff_location="Disney", route=self.route,
            status="confirmed", driver=driver)

    def _board_v(self, vehicle=None, view=None):
        url = reverse("schedule_board") + f"?date={DAY.isoformat()}"
        if view:
            url += f"&view={view}"
        if vehicle is not None:
            url += f"&vehicle={vehicle}"
        return self.client.get(url)

    @staticmethod
    def _opts(resp):
        return [(o["key"], o["count"]) for o in resp.context["board_vehicle_options"]]

    # ── Honest options ──────────────────────────────────────────────────────
    def test_no_backlog_means_no_control(self):
        self._leg(driver=self.george)          # assigned jobs alone don't count
        resp = self._board_v()
        self.assertEqual(self._opts(resp), [])
        self.assertNotContains(resp, 'id="boardVehicleFilter"')

    def test_options_are_the_unassigned_types_with_counts(self):
        self._leg(driver=self.george)                       # towncar, assigned — ignored
        self._leg(pickup_time=time(11, 0))                  # towncar, backlog
        self._leg(pickup_time=time(15, 0))                  # towncar, backlog
        self._van_leg(driver=self.sam)                      # van, assigned — ignored
        self._van_leg(pickup_time=time(12, 0))              # van, backlog
        resp = self._board_v()
        # Fleet order (towncar before van); only the backlog is counted.
        self.assertEqual(self._opts(resp), [("towncar", 2), ("van", 1)])

    def test_ticked_type_with_no_backlog_job_today_stays_listed(self):
        self._leg(pickup_time=time(11, 0))                  # towncar backlog only
        resp = self._board_v(vehicle="van")
        self.assertEqual(resp.context["vehicle_filter"], ["van"])
        self.assertEqual(self._opts(resp), [("towncar", 1), ("van", 0)])

    def test_junk_and_duplicates_are_dropped(self):
        self._van_leg()
        resp = self._board_v(vehicle="van,van,helicopter,,suv")
        self.assertEqual(resp.context["vehicle_filter"], ["van", "suv"])

    # ── The board keeps its shape ───────────────────────────────────────────
    def test_filter_changes_nothing_server_side(self):
        self._leg(driver=self.george)
        self._van_leg(driver=self.sam)
        self._leg(pickup_time=time(13, 0))                  # backlog
        self._van_leg(pickup_time=time(14, 0))              # backlog
        plain = self._board_v()
        filtered = self._board_v(vehicle="van")
        for key in ("total_legs", "assigned_count", "unassigned_count"):
            self.assertEqual(plain.context[key], filtered.context[key], key)
        self.assertEqual(
            [r["driver"].id for r in plain.context["inhouse_timeline"]],
            [r["driver"].id for r in filtered.context["inhouse_timeline"]])
        self.assertEqual(
            len(plain.context["unassigned_timeline_slots"]),
            len(filtered.context["unassigned_timeline_slots"]))
        # Every slot still renders, tagged with its type, for the browser to hide.
        self.assertContains(filtered, 'data-vehicle="towncar"', count=2)
        self.assertContains(filtered, 'data-vehicle="van"', count=2)

    # ── Sticky ──────────────────────────────────────────────────────────────
    def test_choice_rides_the_date_arrows_and_the_board_switch(self):
        self._van_leg()
        resp = self._board_v(vehicle="van,Van(14 Pax)")
        qs = resp.context["vehicle_filter_qs"]
        self.assertEqual(qs, "&vehicle=van%2CVan%2814%20Pax%29")
        html = resp.content.decode()
        # Autoescape turns the joining "&" into "&amp;" inside href attributes.
        href_qs = qs.replace("&", "&amp;")
        self.assertIn(f"&view=inhouse{href_qs}", html)
        self.assertIn(f"&view=affiliate{href_qs}", html)
        # Every link that keeps the filter carries it (the class also appears
        # once in the script's selector, hence the class= prefix).
        self.assertEqual(html.count(href_qs), html.count('class="board-keeps-vehicle'))
        # And the date picker's JS seed carries the same thing.
        self.assertIn(qs.replace("&", "\\u0026").replace("=", "\\u003D"), html)

    def test_control_renders_with_chosen_types_ticked(self):
        self._leg(pickup_time=time(11, 0))
        self._van_leg()
        resp = self._board_v(vehicle="van")
        self.assertContains(resp, 'id="boardVehicleFilter"')
        self.assertContains(resp, 'value="van" checked')
        self.assertNotContains(resp, 'value="towncar" checked')
        self.assertContains(resp, 'bi-car-front-fill')
        # The script only ever touches chips in the Unassigned row.
        self.assertContains(resp, ".dnd-unassigned-row .timeline-slot[data-vehicle]")
