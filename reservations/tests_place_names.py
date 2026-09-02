"""Address tidying for the dispatcher booking wizard.

Two things are being pinned here, and the second one matters more than the first:

  1. The typing damage that tidying is FOR — doubled spaces, a comma with no
     space after it, caps lock left on — is repaired.
  2. Tidying never renames a place. The stored address is the key that
     ``quote_engine.match_location`` prices the trip from, that
     ``analytics.categorize_location`` buckets, and that the wizard's own
     ``short_place`` filter shortens by an exact string replace. A "helpful"
     rewrite of "MCO Airport" into the airport's full name changes which rate
     card the leg matches — a pricing bug with no price field involved.
"""
from datetime import timedelta

from django.test import TestCase
from django.utils import timezone as django_timezone

from dispatching.forms import DispatcherLegForm
from reservations.place_names import MAX_ADDRESS_LENGTH, tidy_address


class TidyAddressTests(TestCase):

    # ── What tidying is for ────────────────────────────────────────────────

    def test_typing_damage_is_repaired(self):
        cases = {
            "  Disney   Springs  ": "Disney Springs",
            "Orlando International Airport (MCO) , Jeff Fuqua Blvd":
                "Orlando International Airport (MCO), Jeff Fuqua Blvd",
            "1234 Sand Lake Rd,Orlando,FL 32819":
                "1234 Sand Lake Rd, Orlando, FL 32819",
            "Disney Pop Century Resort,": "Disney Pop Century Resort",
            "Disney Springs,, Orlando": "Disney Springs, Orlando",
        }
        for raw, expected in cases.items():
            self.assertEqual(tidy_address(raw), expected, raw)

    def test_a_single_case_address_is_written_out_properly(self):
        """Step 5 reads the address aloud in bold — block capitals shout."""
        cases = {
            "PORT CANAVERAL": "Port Canaveral",
            "disney's grand floridian resort & spa":
                "Disney's Grand Floridian Resort & Spa",
            "DISNEY'S GRAND FLORIDIAN RESORT & SPA":
                "Disney's Grand Floridian Resort & Spa",
            "1189 ESPERANZA RIDGE RD CLERMONT, FL 34715":
                "1189 Esperanza Ridge Rd Clermont, FL 34715",
            "123 nw 5th st, orlando, fl": "123 NW 5th St, Orlando, FL",
            "bay lake tower at the contemporary":
                "Bay Lake Tower at the Contemporary",
            "o'brien road": "O'Brien Road",
        }
        for raw, expected in cases.items():
            self.assertEqual(tidy_address(raw), expected, raw)

    def test_an_airport_code_stays_in_capitals(self):
        """"mco" is a code, not a word — lower-casing it hides it from the
        airport detectors that read this field."""
        self.assertEqual(tidy_address("mco"), "MCO")
        self.assertEqual(tidy_address("mco terminal b"), "MCO Terminal B")
        self.assertEqual(tidy_address("sfb"), "SFB")

    def test_a_route_number_keeps_its_shape(self):
        self.assertEqual(tidy_address("i-4 and us-192"), "I-4 and US-192")
        self.assertEqual(tidy_address("a1a beachfront ave"), "A1A Beachfront Ave")

    # ── What tidying must never do ─────────────────────────────────────────

    def test_a_place_is_never_renamed(self):
        """The rate card is matched on these exact words. "MCO Airport" is a
        Location row; expand it and the leg matches nothing."""
        for raw in ("MCO Airport", "Disney Pop Century", "Port Canaveral",
                    "Orlando International Airport (MCO)", "Disney Springs",
                    "Universal Orlando", "Brightline MCO", "Cafe Mia"):
            self.assertEqual(tidy_address(raw), raw)

    def test_an_address_someone_wrote_is_left_alone(self):
        """Mixed case means a person (or the wizard's datalist) chose how this
        reads. Only a single-case address is missing that information."""
        raw = "Disney's Port Orleans Resort - French Quarter, Orlando, FL"
        self.assertEqual(tidy_address(raw), raw)

    def test_the_canonical_mco_label_survives_verbatim(self):
        """booking_filters.short_place and the wizard's shortPlace() both
        shorten by an EXACT replace of this string."""
        mco = "Orlando International Airport (MCO)"
        self.assertIn(mco, tidy_address(f"  {mco}  "))

    # ── Shape of the return value ──────────────────────────────────────────

    def test_it_always_returns_a_string(self):
        """Leg.pickup_location is a non-null CharField, and the form reads the
        field with .get(), which is None when the field never cleaned."""
        for raw in (None, "", "   ", ",", " , , "):
            self.assertEqual(tidy_address(raw), "")

    def test_it_never_outgrows_what_was_typed(self):
        """Putting a space after a comma is the one step that lengthens a value,
        and it is given up rather than overflow the column. The form has already
        held the input to max_length, so a tidied address that is no longer than
        the typed one always fits."""
        raw = "a," * 127
        self.assertLessEqual(len(raw), MAX_ADDRESS_LENGTH)
        tidied = tidy_address(raw)
        self.assertLessEqual(len(tidied), MAX_ADDRESS_LENGTH)
        self.assertLessEqual(len(tidied), len(raw))
        # Short values still get the spacing.
        self.assertEqual(tidy_address("a,b"), "A, B")


class LegFormTidiesAddressesTests(TestCase):
    """The contract at its real call site."""

    def _form(self, pickup, dropoff):
        return DispatcherLegForm({
            "pickup_date": (django_timezone.localdate() + timedelta(days=30)).isoformat(),
            "pickup_time": "14:30",
            "pickup_location": pickup,
            "dropoff_location": dropoff,
            "private_notes": "",
        })

    def test_both_addresses_are_tidied_on_the_way_in(self):
        form = self._form("  mco   terminal b ", "disney's grand floridian resort")
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["pickup_location"], "MCO Terminal B")
        self.assertEqual(form.cleaned_data["dropoff_location"],
                         "Disney's Grand Floridian Resort")

    def test_a_rate_card_name_reaches_the_database_unchanged(self):
        form = self._form("MCO Airport", "Disney Pop Century")
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["pickup_location"], "MCO Airport")
        self.assertEqual(form.cleaned_data["dropoff_location"], "Disney Pop Century")
