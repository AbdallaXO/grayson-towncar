"""A quote request is priced the moment it comes in, and the task carries it.

Founder ask 2026-09-21: "just get the price and have it in the text." The
quote calculator's engine prices any address pair, so the number is not a
judgement call. ops/quote_pricing.py works it out in the background right
after the QUOTE NEEDED task is filed and writes it onto the task; the task
page opens with the text already priced and the queue title carries the
figure. Pinned here:

  * the price, its source, distance and breakdown land in the task metadata,
    the title gains "· $185", the description gains a "Suggested price" line;
  * re-pricing replaces the figure rather than stacking a second one;
  * an engine error is stored as `price_error` and nothing else changes;
  * a request with no route is left alone with a reason;
  * the task page hands the stored price to the compose card;
  * the public quote form schedules the pricing for the task it files;
  * Lead.estimated_price stays empty — the website did NOT quote this.
"""
import json
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from ops.models import OperationalTask
from ops.quote_pricing import money, price_quote_task
from rates.models import Vehicle
from reservations.models import Lead


def _payload(price="185.00", **extra):
    base = {
        "price": price, "source_label": "Custom estimate", "card_route": None,
        "distance_text": "41.2 mi", "duration_text": "52 mins",
        "internal": {"base_fare": "60.00", "per_mile": "3.00", "direction": "outbound"},
        "notes": ["Long trip: priced from base."], "gratuity_mandatory": False,
    }
    base.update(extra)
    return base


@override_settings(GHL_API_KEY="", GHL_LOCATION_ID="")
class QuotePricingTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.staff = User.objects.create_user(
            username="qp_disp", password="x", is_staff=True, first_name="Dana")
        cls.suv = Vehicle.objects.create(vehicle_type="suv", capacity=6, luggage_capacity=6)

    def setUp(self):
        p = patch("reservations.utils._run_in_background", lambda fn, *a, **k: None)
        p.start()
        self.addCleanup(p.stop)

    def _lead(self, **kw):
        defaults = dict(
            first_name="Angeline", last_name="Owens", phone="17734167497",
            pickup_location="Sanford Int'l Airport",
            dropoff_location="Orlando International Airport",
            pickup_date=timezone.localdate() + timedelta(days=9),
            trip_type="oneway", vehicle=self.suv)
        defaults.update(kw)
        return Lead.objects.create(**defaults)

    def _task(self, lead):
        return OperationalTask.objects.create(
            task_type=OperationalTask.TaskType.MANUAL,
            title="QUOTE NEEDED — Angeline Owens: Sanford Int'l Airport → Orlando International Airport",
            description=(
                "Custom route with no online rate — send this guest a price.\n\n"
                "Route: Sanford Int'l Airport → Orlando International Airport\n"
                "Trip: One way"
            ),
            priority=OperationalTask.Priority.HIGH, due_at=timezone.now(),
            lead=lead, metadata={"source": "quote_form_no_rate"})

    def test_money_drops_needless_cents(self):
        self.assertEqual(money("185.00"), "185")
        self.assertEqual(money("187.50"), "187.50")
        self.assertEqual(money(Decimal("90")), "90")

    def test_the_price_lands_on_the_task(self):
        task = self._task(self._lead())
        with patch("dispatching.views.price_trip", return_value=_payload()) as engine:
            result = price_quote_task(task.id)
        self.assertIsNotNone(result)
        engine.assert_called_once_with(
            "Sanford Int'l Airport", "Orlando International Airport", "suv", "oneway",
            use_cache=True)
        task.refresh_from_db()
        meta = task.metadata
        self.assertEqual(meta["suggested_price"], "185")
        self.assertEqual(meta["price_source_label"], "Custom estimate")
        self.assertEqual(meta["distance_text"], "41.2 mi")
        self.assertEqual(meta["price_internal"]["base_fare"], "60.00")
        self.assertEqual(meta["priced_vehicle"], "suv")
        self.assertNotIn("price_error", meta)
        self.assertTrue(task.title.endswith(" · $185"))
        self.assertIn("Suggested price: $185 (Custom estimate, 41.2 mi, 52 mins)", task.description)
        # Still a task about a route the website could not quote.
        task.lead.refresh_from_db()
        self.assertIsNone(task.lead.estimated_price)

    def test_a_round_trip_lead_is_priced_as_one(self):
        task = self._task(self._lead(trip_type="roundtrip", vehicle=None))
        with patch("dispatching.views.price_trip", return_value=_payload()) as engine:
            price_quote_task(task.id)
        self.assertEqual(engine.call_args.args[2:], ("towncar", "roundtrip"))

    def test_repricing_replaces_the_figure(self):
        task = self._task(self._lead())
        with patch("dispatching.views.price_trip", return_value=_payload("185.00")):
            price_quote_task(task.id)
        with patch("dispatching.views.price_trip", return_value=_payload("210.00")):
            price_quote_task(task.id)
        task.refresh_from_db()
        self.assertEqual(task.title.count(" · $"), 1)
        self.assertTrue(task.title.endswith(" · $210"))
        self.assertEqual(task.description.count("Suggested price:"), 1)
        self.assertIn("$210", task.description)
        self.assertEqual(task.metadata["suggested_price"], "210")

    def test_an_engine_error_is_stored_and_nothing_else_changes(self):
        task = self._task(self._lead())
        with patch("dispatching.views.price_trip",
                   return_value={"error": "Could not calculate distance. Check the addresses and try again."}):
            self.assertIsNone(price_quote_task(task.id))
        task.refresh_from_db()
        self.assertIn("Could not calculate distance", task.metadata["price_error"])
        self.assertNotIn("suggested_price", task.metadata)
        self.assertNotIn(" · $", task.title)
        self.assertNotIn("Suggested price", task.description)

    def test_a_request_with_no_route_is_left_with_a_reason(self):
        task = self._task(self._lead(dropoff_location=""))
        with patch("dispatching.views.price_trip") as engine:
            self.assertIsNone(price_quote_task(task.id))
        engine.assert_not_called()
        task.refresh_from_db()
        self.assertEqual(task.metadata["price_error"], "The request has no route to price.")

    def test_the_page_hands_the_stored_price_to_the_compose_card(self):
        task = self._task(self._lead())
        with patch("dispatching.views.price_trip", return_value=_payload()):
            price_quote_task(task.id)
        self.client.force_login(self.staff)
        resp = self.client.get(reverse("task_detail", args=[task.id]))
        compose = resp.context["qn_compose"]
        self.assertEqual(compose["priced"]["price"], "185")
        self.assertEqual(compose["priced"]["source_label"], "Custom estimate")
        self.assertEqual(compose["priced"]["internal"]["per_mile"], "3.00")
        self.assertIsNone(compose["price_error"])
        # The live request is still there as the fallback for an unpriced task.
        self.assertEqual(compose["price_request"]["vehicle"], "suv")

    def test_an_unpriced_task_tells_the_page_why(self):
        task = self._task(self._lead())
        with patch("dispatching.views.price_trip", return_value={"error": "Could not price that trip."}):
            price_quote_task(task.id)
        self.client.force_login(self.staff)
        compose = self.client.get(reverse("task_detail", args=[task.id])).context["qn_compose"]
        self.assertIsNone(compose["priced"])
        self.assertEqual(compose["price_error"], "Could not price that trip.")


@override_settings(GHL_API_KEY="", GHL_LOCATION_ID="")
class QuoteFormSchedulesPricingTests(TestCase):
    """The public quote form files the task and hands it to the pricer."""

    @classmethod
    def setUpTestData(cls):
        cls.suv = Vehicle.objects.create(vehicle_type="suv", capacity=6, luggage_capacity=6)

    def setUp(self):
        p = patch("reservations.utils._run_in_background", lambda fn, *a, **k: None)
        p.start()
        self.addCleanup(p.stop)

    def test_a_custom_route_request_files_a_task_and_prices_it(self):
        body = {
            "first_name": "Angeline", "last_name": "Owens",
            "email": "angeline@example.com", "phone": "7734167497",
            "pickup_location": "Sanford Int'l Airport",
            "dropoff_location": "Orlando International Airport",
            "pickup_date": (timezone.localdate() + timedelta(days=9)).isoformat(),
            "trip_type": "1", "vehicle_id": self.suv.id, "estimated_price": None,
        }
        with patch("ops.quote_pricing.price_quote_task_in_background") as scheduled:
            resp = self.client.post(
                reverse("quote_form_handler"), json.dumps(body),
                content_type="application/json")
        self.assertEqual(resp.status_code, 200, resp.content[:200])
        task = OperationalTask.objects.get(
            task_type=OperationalTask.TaskType.MANUAL, title__startswith="QUOTE NEEDED")
        scheduled.assert_called_once_with(task.id)
        self.assertEqual(task.lead.first_name, "Angeline")
        self.assertIsNone(task.lead.estimated_price)
