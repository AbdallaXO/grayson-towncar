"""
Quote email tests — the email must describe the trip it priced.

The price in a quote email comes from the lead's current Quote. Every other
line — vehicle, trip type, route, date — used to come from the Lead itself,
and the two drift apart the moment a returning customer asks for a second
quote: reservations/views.py makes a NEW Quote with the new vehicle, but only
updates the Lead's own vehicle when the trip changed. So the Lead kept the old
vehicle while the Quote carried the new price, and the email paired them.

That shipped a $315 round-trip Sanford towncar quote to a guest under the
heading "Mini Van" (lead #48154, 14 Sep 2026). 2,832 guests had already been
sent a wrong-vehicle quote by the time it was found.

The rule these tests hold: when there is a current Quote, every trip detail in
the email comes from that Quote. The Lead is only a fallback.

These render the template directly rather than going through
``send_lead_quote_email``, which hands off to a background thread and so makes
``mail.outbox`` racy. The defect is in what gets rendered, so that is what is
asserted.
"""

from datetime import date
from decimal import Decimal

from django.template.loader import render_to_string
from django.test import TestCase

from rates.models import Vehicle
from reservations.models import Lead, Quote
from users.emails import build_lead_quote_context


class QuoteEmailDescribesThePricedTrip(TestCase):
    """The vehicle, trip type, route and date shown must match the price shown."""

    def setUp(self):
        self.towncar = Vehicle.objects.create(
            vehicle_type="towncar", capacity=3, luggage_capacity=3
        )
        self.minivan = Vehicle.objects.create(
            vehicle_type="mini_van", capacity=6, luggage_capacity=6
        )
        # A returning guest: the Lead still carries the minivan they asked
        # about last time, and that old trip's route and date.
        self.lead = Lead.objects.create(
            first_name="Casey",
            email="casey@example.com",
            vehicle=self.minivan,
            pickup_location="Orlando International Airport",
            dropoff_location="Universal Studios Area Hotels",
            trip_type="oneway",
            pickup_date=date(2026, 10, 1),
        )

    def _quote(self, **overrides):
        fields = dict(
            lead=self.lead,
            vehicle=self.towncar,
            estimated_price=Decimal("315.00"),
            pickup_location="Sanford Int'l Airport",
            dropoff_location="All WDW Disney Property Resorts",
            trip_type="roundtrip",
            pickup_date=date(2026, 11, 20),
            is_current=True,
        )
        fields.update(overrides)
        return Quote.objects.create(**fields)

    def _render(self):
        return render_to_string(
            "users/lead_quote_email.html", build_lead_quote_context(self.lead)
        )

    def test_vehicle_shown_is_the_one_the_price_was_quoted_for(self):
        self._quote()

        body = self._render()

        self.assertIn("315.00", body)
        self.assertIn("Towncar", body)
        self.assertNotIn("Mini Van", body)

    def test_trip_type_shown_is_the_quoted_one(self):
        self._quote()

        body = self._render()

        self.assertIn("Round Trip", body)
        self.assertNotIn("One Way", body)

    def test_route_shown_is_the_quoted_one(self):
        self._quote()

        body = self._render()

        self.assertIn("Sanford", body)
        self.assertIn("All WDW Disney Property Resorts", body)
        self.assertNotIn("Universal Studios Area Hotels", body)

    def test_date_shown_is_the_quoted_one(self):
        self._quote()

        body = self._render()

        self.assertIn("November 20, 2026", body)
        self.assertNotIn("October 01, 2026", body)

    def test_lead_details_are_used_when_there_is_no_quote(self):
        """No Quote row at all — the Lead is still the best we have."""
        body = self._render()

        self.assertIn("Mini Van", body)
        self.assertIn("Universal Studios Area Hotels", body)

    def test_quote_without_a_vehicle_falls_back_to_the_lead(self):
        """A quote can be priced with no vehicle attached; don't blank the line."""
        self._quote(vehicle=None)

        body = self._render()

        self.assertIn("Mini Van", body)

    def test_subject_line_names_the_quoted_route(self):
        """The subject is the first thing the guest reads — same rule applies."""
        self._quote()

        subject = build_lead_quote_context(self.lead)["subject"]

        self.assertIn("Sanford", subject)
        self.assertNotIn("Universal Studios Area Hotels", subject)

    def test_superseded_quote_is_ignored_in_favour_of_the_current_one(self):
        """is_current is what picks the quote, not insertion order."""
        self._quote(vehicle=self.minivan, estimated_price=Decimal("230.00"))
        # Quote.save() unmarks the previous current quote.
        self._quote(vehicle=self.towncar, estimated_price=Decimal("315.00"))

        body = self._render()

        self.assertIn("315.00", body)
        self.assertIn("Towncar", body)
        self.assertNotIn("Mini Van", body)
