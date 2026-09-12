"""The schedule board's passenger search — name/phone/email/50-number in, a day out.

Run with:  ./manage.py test dispatching.tests_board_passenger_search

The feature exists so a dispatcher with a guest on the phone stops leaving the
board to find out which day that guest is on. That only works if:

  * the guest is found however the dispatcher types the number — the phone book
    holds "(407) 555-1234", "+1 407-555-1234" and "4075551234" for the same kind
    of person, and they will type whichever one is on their screen;
  * the link lands on the board the trip is actually drawn on — a farmed-out job
    is on the affiliate board, and sending someone to the in-house board for it
    shows them an empty lane and no explanation;
  * a cancelled trip is returned SAYING it is cancelled and carrying no board
    link, because "it isn't on the board" is the answer, not a bug;
  * the guest whose trip is nearest sorts first — that is the one they are
    holding the phone about;
  * and when the ?focus= landing can't ring the trip, the board says why.
"""

from datetime import time, timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from drivers.models import Driver
from rates.models import Location, Rate, Route, Vehicle
from reservations.models import Customer, Leg, Reservation

TODAY = timezone.localdate()
SOON = TODAY + timedelta(days=3)


class _SearchFixture(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.staff = User.objects.create_user("dispatcher", password="x", is_staff=True)
        cls.vehicle = Vehicle.objects.create(
            vehicle_type="towncar", capacity=4, luggage_capacity=4)
        cls.route = Route.objects.create(
            origin=Location.objects.create(name="MCO"),
            destination=Location.objects.create(name="Disney"),
            inhouse_base_pay=Decimal("50.00"))
        cls.rate = Rate.objects.create(
            vehicle=cls.vehicle, route=cls.route,
            oneway_price=Decimal("100.00"), round_trip_price=Decimal("180.00"))

        cls.sam = Driver.objects.create(
            profile=User.objects.create_user("bps_sam", first_name="Sam",
                                             last_name="Reed"),
            driver_type="inhouse")
        cls.waleed = Driver.objects.create(
            profile=User.objects.create_user("bps_waleed", first_name="Waleed",
                                             last_name="Nasser"),
            driver_type="affiliate")

    @classmethod
    def customer(cls, first, last, email, phone):
        return Customer.objects.create(
            first_name=first, last_name=last, email=email, phone_number=phone)

    @classmethod
    def trip(cls, customer, day, hhmm, driver=None, status=None,
             reservation_status="confirmed", res_id=None,
             pickup="Orlando International Airport (MCO), Jeff Fuqua Blvd, Orlando, FL",
             dropoff="Disney Grand Floridian, Orlando, FL"):
        # res_id is pinned where the test is about the number a guest reads out;
        # a real reservation id is four or five digits, never the "1" an empty
        # test database would hand back.
        reservation = Reservation.objects.create(
            customer=customer, vehicle=cls.vehicle, rate=cls.rate,
            trip_type="one-way", status=reservation_status,
            base_price=Decimal("100.00"), total_price=Decimal("100.00"),
            **({"id": res_id} if res_id else {}))
        hour, minute = (int(part) for part in hhmm.split(":"))
        return Leg.objects.create(
            reservation=reservation, pickup_date=day, pickup_time=time(hour, minute),
            driver=driver, status=status,
            pickup_location=pickup, dropoff_location=dropoff)

    def search(self, query):
        self.client.force_login(self.staff)
        response = self.client.get(reverse("board_passenger_search"), {"q": query})
        self.assertEqual(response.status_code, 200)
        return response.json()

    @staticmethod
    def names(payload):
        return [person["name"] for person in payload["results"]]


class AccessTests(_SearchFixture):
    def test_a_signed_out_visitor_is_sent_to_the_login_page(self):
        response = self.client.get(reverse("board_passenger_search"), {"q": "george"})
        self.assertEqual(response.status_code, 302)

    def test_a_signed_in_non_staff_user_gets_nothing(self):
        # Guests have accounts. The board's search reaches every guest on file,
        # so staff-only is the whole security model here.
        User.objects.create_user("guest", password="x")
        self.client.force_login(User.objects.get(username="guest"))
        response = self.client.get(reverse("board_passenger_search"), {"q": "george"})
        self.assertEqual(response.status_code, 403)

    def test_one_letter_answers_empty_instead_of_scanning_the_table(self):
        self.customer("George", "Harrison", "george@example.com", "4075551234")
        payload = self.search("g")
        self.assertTrue(payload["too_short"])
        self.assertEqual(payload["results"], [])


class MatchingTests(_SearchFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.george = cls.customer("George", "Harrison", "george.h@example.com",
                                  "(407) 555-1234")
        cls.leg = cls.trip(cls.george, SOON, "10:15", driver=cls.sam, res_id=4321)

    def test_first_name_finds_the_guest(self):
        self.assertEqual(self.names(self.search("george")), ["George Harrison"])

    def test_last_name_finds_the_guest(self):
        self.assertEqual(self.names(self.search("harrison")), ["George Harrison"])

    def test_full_name_finds_the_guest(self):
        self.assertEqual(self.names(self.search("george harrison")),
                         ["George Harrison"])

    def test_email_fragment_finds_the_guest(self):
        self.assertEqual(self.names(self.search("george.h@")), ["George Harrison"])

    def test_bare_digits_find_a_number_stored_with_formatting(self):
        # Typed off a caller ID; stored as "(407) 555-1234".
        self.assertEqual(self.names(self.search("4075551234")), ["George Harrison"])

    def test_a_formatted_number_finds_one_stored_bare(self):
        self.customer("Ringo", "Starr", "ringo@example.com", "4075559999")
        self.assertEqual(self.names(self.search("(407) 555-9999")), ["Ringo Starr"])

    def test_the_last_seven_digits_are_enough(self):
        self.assertEqual(self.names(self.search("555-1234")), ["George Harrison"])

    def test_the_fifty_number_off_the_confirmation_finds_the_guest(self):
        # The guest reads out "501234" and never says their name.
        self.assertEqual(self.leg.reservation.display_number, "504321")
        payload = self.search("504321")
        self.assertEqual(self.names(payload), ["George Harrison"])

    def test_the_bare_reservation_id_works_too(self):
        # Some dispatchers drop the 50 out of habit.
        payload = self.search("4321")
        self.assertEqual(self.names(payload), ["George Harrison"])

    def test_an_email_with_digits_in_it_is_not_read_as_a_phone_number(self):
        # Real guests have addresses like nkf2014@gmail.com. Treating the "2014"
        # as a phone fragment used to hand back every guest whose NUMBER
        # contained 2014 — and not the guest whose address it is.
        self.customer("Nick", "Kruger", "nkf2014@gmail.com", "952-688-3554")
        self.customer("Wrong", "Person", "wrong@example.com", "407-555-2014")
        self.assertEqual(self.names(self.search("nkf2014")), ["Nick Kruger"])

    def test_a_name_with_a_digit_in_it_still_searches_by_name(self):
        self.customer("Trip", "Smith2", "smith2@example.com", "4075558888")
        self.assertEqual(self.names(self.search("smith2")), ["Trip Smith2"])

    def test_a_name_nobody_has_comes_back_empty_not_broken(self):
        self.assertEqual(self.search("zzzznobody")["results"], [])


class TripRowTests(_SearchFixture):
    def test_the_link_carries_the_day_the_board_and_the_trip(self):
        george = self.customer("George", "Harrison", "g@example.com", "4075551234")
        leg = self.trip(george, SOON, "10:15", driver=self.sam)

        trip = self.search("george")["results"][0]["trips"][0]
        self.assertEqual(trip["date"], SOON.isoformat())
        self.assertEqual(trip["board"], "inhouse")
        self.assertEqual(trip["driver"], "Sam Reed")
        self.assertIn(f"date={SOON.isoformat()}", trip["board_url"])
        self.assertIn("view=inhouse", trip["board_url"])
        self.assertIn(f"focus={leg.id}", trip["board_url"])

    def test_a_farmed_out_trip_points_at_the_affiliate_board(self):
        # The in-house board would show an empty lane for this one.
        paul = self.customer("Paul", "McCartney", "paul@example.com", "4075552222")
        self.trip(paul, SOON, "09:00", driver=self.waleed)

        trip = self.search("mccartney")["results"][0]["trips"][0]
        self.assertEqual(trip["board"], "affiliate")
        self.assertIn("view=affiliate", trip["board_url"])

    def test_an_unassigned_trip_still_lands_on_the_in_house_board(self):
        # Unassigned jobs sit in the shared lane at the top of the in-house board.
        john = self.customer("John", "Lennon", "john@example.com", "4075553333")
        self.trip(john, SOON, "08:00", driver=None)

        trip = self.search("lennon")["results"][0]["trips"][0]
        self.assertEqual(trip["board"], "inhouse")
        self.assertEqual(trip["driver"], "")

    def test_a_cancelled_trip_is_marked_and_carries_no_board_link(self):
        ringo = self.customer("Ringo", "Starr", "ringo@example.com", "4075554444")
        self.trip(ringo, SOON, "07:00", driver=self.sam,
                  reservation_status="cancelled")

        trip = self.search("ringo")["results"][0]["trips"][0]
        self.assertTrue(trip["cancelled"])
        self.assertEqual(trip["board_url"], "")
        self.assertTrue(trip["res_url"])

    def test_todays_trip_is_labelled_today(self):
        george = self.customer("George", "Harrison", "g@example.com", "4075551234")
        self.trip(george, TODAY, "10:15", driver=self.sam)
        self.assertEqual(self.search("george")["results"][0]["trips"][0]["when"],
                         "today")

    def test_the_route_reads_as_landmarks_not_as_addresses(self):
        george = self.customer("George", "Harrison", "g@example.com", "4075551234")
        self.trip(george, SOON, "10:15", driver=self.sam)
        self.assertEqual(self.search("george")["results"][0]["trips"][0]["route"],
                         "MCO → Disney Grand Floridian")


class RankingTests(_SearchFixture):
    def test_the_guest_with_a_trip_coming_sorts_above_one_with_only_history(self):
        # Two Harrisons. The dispatcher is on the phone about the one flying in
        # on Thursday, never about the one who rode last spring.
        old = self.customer("Olivia", "Harrison", "olivia@example.com", "4075550001")
        self.trip(old, TODAY - timedelta(days=40), "12:00", driver=self.sam)
        coming = self.customer("George", "Harrison", "george@example.com", "4075550002")
        self.trip(coming, SOON, "10:15", driver=self.sam)

        self.assertEqual(self.names(self.search("harrison"))[0], "George Harrison")

    def test_a_guests_soonest_trip_is_listed_first(self):
        george = self.customer("George", "Harrison", "g@example.com", "4075551234")
        self.trip(george, SOON + timedelta(days=10), "16:00", driver=self.sam)
        self.trip(george, SOON, "10:15", driver=self.sam)

        trips = self.search("george")["results"][0]["trips"]
        self.assertEqual(trips[0]["date"], SOON.isoformat())

    def test_history_older_than_six_months_stays_out_of_the_panel(self):
        george = self.customer("George", "Harrison", "g@example.com", "4075551234")
        self.trip(george, TODAY - timedelta(days=400), "12:00", driver=self.sam)

        person = self.search("george")["results"][0]
        self.assertEqual(person["trips"], [])
        # ...but the panel still says the history exists, so nobody reads the
        # empty list as "this guest has never ridden with us".
        self.assertEqual(person["trip_count"], 1)


class BoardFocusTests(_SearchFixture):
    """Landing on the board with ?focus=<leg> — the ring, and the honest miss."""

    def board(self, **params):
        self.client.force_login(self.staff)
        response = self.client.get(reverse("schedule_board"), params)
        self.assertEqual(response.status_code, 200)
        return response

    def test_the_board_rings_the_trip_you_searched_for(self):
        george = self.customer("George", "Harrison", "g@example.com", "4075551234")
        leg = self.trip(george, SOON, "10:15", driver=self.sam)

        response = self.board(date=SOON.isoformat(), view="inhouse", focus=leg.id)
        self.assertEqual(response.context["focus_leg_id"], leg.id)
        self.assertContains(response, "George Harrison")

    def test_a_trip_on_another_day_is_named_with_the_way_to_it(self):
        # The trip moved after the dispatcher opened the search. Silence here
        # would read as "the search lied".
        george = self.customer("George", "Harrison", "g@example.com", "4075551234")
        leg = self.trip(george, SOON, "10:15", driver=self.sam)

        response = self.board(date=TODAY.isoformat(), view="inhouse", focus=leg.id)
        self.assertIsNone(response.context["focus_leg_id"])
        note = response.context["focus_note"]
        self.assertFalse(note["on_this_board"])
        self.assertIn(f"date={SOON.isoformat()}", note["correct_url"])
        self.assertIn("Take me to it", response.content.decode())

    def test_a_farmed_out_trip_is_not_rung_on_the_in_house_board(self):
        paul = self.customer("Paul", "McCartney", "paul@example.com", "4075552222")
        leg = self.trip(paul, SOON, "09:00", driver=self.waleed)

        response = self.board(date=SOON.isoformat(), view="inhouse", focus=leg.id)
        self.assertIsNone(response.context["focus_leg_id"])
        self.assertIn("view=affiliate", response.context["focus_note"]["correct_url"])

    def test_a_cancelled_trip_says_so_instead_of_hunting_for_a_bar(self):
        ringo = self.customer("Ringo", "Starr", "ringo@example.com", "4075554444")
        leg = self.trip(ringo, SOON, "07:00", driver=self.sam,
                        reservation_status="cancelled")

        response = self.board(date=SOON.isoformat(), view="inhouse", focus=leg.id)
        self.assertIsNone(response.context["focus_leg_id"])
        self.assertTrue(response.context["focus_note"]["cancelled"])
        self.assertContains(response, "is cancelled")

    def test_a_focus_on_a_leg_that_no_longer_exists_changes_nothing(self):
        response = self.board(date=SOON.isoformat(), view="inhouse", focus=99999999)
        self.assertIsNone(response.context["focus_leg_id"])
        self.assertIsNone(response.context["focus_note"])

    def test_junk_in_the_focus_parameter_changes_nothing(self):
        response = self.board(date=SOON.isoformat(), view="inhouse", focus="drop-table")
        self.assertIsNone(response.context["focus_leg_id"])
        self.assertIsNone(response.context["focus_note"])

    def test_the_search_box_is_on_the_board(self):
        response = self.board(date=SOON.isoformat())
        self.assertContains(response, 'id="boardSearchInput"')
