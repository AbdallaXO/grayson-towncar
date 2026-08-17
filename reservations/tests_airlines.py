"""The airline registry: free text in, one IATA code out.

Every downstream consumer keys off the normalized code — AeroAPI idents, the
tracker deep links, the display name on the board. An airline the registry
doesn't know is stored verbatim, which splits it into as many "airlines" as
there are spellings and silently drops flight tracking for all of them.
"""

from django.test import SimpleTestCase, TestCase

from reservations.models import Flight
from reservations.utils import (
    AIRLINES,
    extract_airline_from_flight_number,
    get_airline_display_name,
    get_flightaware_code,
    normalize_airline,
)


class PorterAirlinesTests(SimpleTestCase):
    """Porter is PD to the world and POE to FlightAware."""

    def test_every_spelling_in_the_data_normalizes_to_pd(self):
        # PORTER / PORTER AIRLINES / PORTER AIRLINE all exist in stored
        # flights, written by whoever took the booking.
        for written in (
            "PD", "pd", "Porter", "PORTER", "porter airlines",
            "Porter Airline", "Porter Air", " Porter  Airlines ",
        ):
            self.assertEqual(normalize_airline(written), "PD", msg=written)

    def test_display_name(self):
        self.assertEqual(get_airline_display_name("PD"), "Porter Airlines")

    def test_flightaware_uses_the_icao_code(self):
        self.assertEqual(get_flightaware_code("PD"), "POE")

    def test_code_is_stripped_off_a_prefixed_flight_number(self):
        self.assertEqual(extract_airline_from_flight_number("PD580"), "PD")

    def test_in_the_booking_form_picker(self):
        self.assertIn("Porter Airlines", AIRLINES)


class DiscoverAirlinesTests(SimpleTestCase):
    """Discover is 4Y — a code with a digit in it, so it exercises the paths
    that assume two letters."""

    def test_every_spelling_normalizes_to_4y(self):
        for written in (
            "4Y", "4y", "Discover", "DISCOVER AIRLINES", "discover airlines",
            "Eurowings Discover",  # the name it flew under until Sep 2023
        ):
            self.assertEqual(normalize_airline(written), "4Y", msg=written)

    def test_display_name(self):
        self.assertEqual(get_airline_display_name("4Y"), "Discover Airlines")

    def test_flightaware_uses_the_icao_code(self):
        self.assertEqual(get_flightaware_code("4Y"), "OCN")

    def test_code_is_stripped_off_a_prefixed_flight_number(self):
        self.assertEqual(extract_airline_from_flight_number("4Y111"), "4Y")


class AirlineRegistryRegressionTests(SimpleTestCase):
    """The new entries sit at the end of the mapping so nothing that already
    resolved can be captured by them."""

    def test_existing_carriers_are_untouched(self):
        for written, code in (
            ("Southwest Airlines", "WN"), ("jet blue", "B6"),
            ("Delta Air Lines", "DL"), ("Allegiant Air", "G4"),
            ("Air Canada", "AC"), ("WestJet", "WS"),
        ):
            self.assertEqual(normalize_airline(written), code, msg=written)

    def test_an_unknown_airline_is_still_passed_through_uppercased(self):
        # Deliberate: get_flight_ident()'s guard rejects anything longer than
        # 3 chars rather than sending garbage to AeroAPI.
        self.assertEqual(normalize_airline("Icelandair"), "ICELANDAIR")

    def test_a_plain_number_extracts_no_airline(self):
        self.assertIsNone(extract_airline_from_flight_number("4123"))
        self.assertIsNone(extract_airline_from_flight_number("580"))


class FlightSaveTests(TestCase):
    """What the dispatcher types is what gets stored, normalized."""

    def test_porter_written_as_a_name_becomes_a_tracked_flight(self):
        flight = Flight.objects.create(airline="Porter Airlines",
                                       flight_number="PD580")
        self.assertEqual(flight.airline, "PD")
        self.assertEqual(flight.airline_display_name, "Porter Airlines")
        self.assertEqual(flight.flight_number, "580")
        self.assertEqual(flight.get_flight_ident(), "POE580")

    def test_discover_written_as_a_name_becomes_a_tracked_flight(self):
        flight = Flight.objects.create(airline="Discover Airlines",
                                       flight_number="4Y111")
        self.assertEqual(flight.airline, "4Y")
        self.assertEqual(flight.airline_display_name, "Discover Airlines")
        self.assertEqual(flight.flight_number, "111")
        self.assertEqual(flight.get_flight_ident(), "OCN111")

    def test_a_stale_display_name_is_corrected_on_save(self):
        # Rows exist holding airline "PD" with the display name of whatever
        # carrier the booking said first ("Air Canada"), because an unknown
        # code left the old name in place.
        flight = Flight.objects.create(airline="PD", flight_number="580",
                                       airline_display_name="Air Canada")
        self.assertEqual(flight.airline_display_name, "Porter Airlines")
