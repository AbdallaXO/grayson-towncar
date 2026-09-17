"""
After-hours fee: don't chase money the booking already carries.

``leg.afterhours_fee`` was meant to record "this leg's $20 is collected", but it
is a second copy of a fact the money already states, and it drifts:

  * legs are created with it at 0 even when the booking itemised the $20
    (leg 17662 / reservation #10275 — a web booking with additional_charges = $20
    whose marker read 0 from the creation row onward);
  * the dispatcher booking path only stamps it when ``additional_charges``
    happens to cover the fee, so a fee folded into the base price leaves it 0;
  * a reservation edit re-runs that same test and can re-zero a set marker.

The result was dispatchers being asked to collect a fee the guest had already
paid — 49 tasks raised against 16 trips that provably paid the standard rate
plus $20, 28 of which a human had to open and close. Nobody double-charged;
they just absorbed the interruption and closed it.

So the question the code asks changes from "is the marker set?" to "has this
been collected?", and the booking's own itemised charges answer it.

The genuinely-unbilled case is deliberately preserved: a late booking whose
reservation carries no after-hours money still flags, because that fee really
is owed.
"""

from datetime import date, time
from decimal import Decimal

from django.test import TestCase

from rates.models import Location, Rate, Route, Vehicle
from reservations.models import Customer, Leg, Reservation
from reservations.utils import AFTERHOURS_FEE_AMOUNT


class _Fixture:
    @classmethod
    def setUpTestData(cls):
        cls.vehicle = Vehicle.objects.create(
            vehicle_type="towncar", capacity=3, luggage_capacity=3
        )
        origin = Location.objects.create(name="MCO")
        dest = Location.objects.create(name="Disney")
        cls.route = Route.objects.create(
            origin=origin, destination=dest, inhouse_base_pay=Decimal("50.00")
        )
        cls.rate = Rate.objects.create(
            vehicle=cls.vehicle, route=cls.route,
            oneway_price=Decimal("105.00"), round_trip_price=Decimal("195.00"),
        )
        cls.customer = Customer.objects.create(
            first_name="Sam", last_name="Doe", email="sam@example.com",
            phone_number="5551234567",
        )

    def _res(self, base=Decimal("195.00"), additional=Decimal("0.00")):
        return Reservation.objects.create(
            trip_type="round_trip", customer=self.customer, rate=self.rate,
            vehicle=self.vehicle, base_price=base,
            additional_charges=additional, total_price=base + additional,
        )

    def _leg(self, res, **kw):
        defaults = dict(
            reservation=res, pickup_date=date(2026, 6, 1), pickup_time=time(23, 30),
            pickup_location="MCO", dropoff_location="Disney", route=self.route,
            status="confirmed", afterhours_fee=Decimal("0.00"),
        )
        defaults.update(kw)
        return Leg.objects.create(**defaults)


class ItemisedFeeIsNotChased(_Fixture, TestCase):
    """The $20 sitting in additional_charges settles the fee on its own."""

    def test_late_leg_whose_booking_itemised_the_fee_is_not_owed(self):
        res = self._res(additional=AFTERHOURS_FEE_AMOUNT)
        leg = self._leg(res)

        self.assertEqual(leg.afterhours_fee_outstanding(), Decimal("0.00"))

    def test_late_leg_with_no_after_hours_money_is_still_owed(self):
        """The genuinely-unbilled case must keep flagging."""
        res = self._res(additional=Decimal("0.00"))
        leg = self._leg(res)

        self.assertEqual(leg.afterhours_fee_outstanding(), AFTERHOURS_FEE_AMOUNT)

    def test_two_late_legs_need_two_fees_covered(self):
        """$20 of charges does not settle two late legs."""
        res = self._res(additional=AFTERHOURS_FEE_AMOUNT)
        leg1 = self._leg(res)
        leg2 = self._leg(res, pickup_date=date(2026, 6, 8))

        self.assertEqual(leg1.afterhours_fee_outstanding(), AFTERHOURS_FEE_AMOUNT)
        self.assertEqual(leg2.afterhours_fee_outstanding(), AFTERHOURS_FEE_AMOUNT)

    def test_two_late_legs_both_covered_are_settled(self):
        res = self._res(additional=AFTERHOURS_FEE_AMOUNT * 2)
        leg1 = self._leg(res)
        leg2 = self._leg(res, pickup_date=date(2026, 6, 8))

        self.assertEqual(leg1.afterhours_fee_outstanding(), Decimal("0.00"))
        self.assertEqual(leg2.afterhours_fee_outstanding(), Decimal("0.00"))

    def test_charges_only_settle_the_legs_that_are_actually_late(self):
        """A daytime leg doesn't consume the after-hours budget."""
        res = self._res(additional=AFTERHOURS_FEE_AMOUNT)
        daytime = self._leg(res, pickup_time=time(9, 0))
        late = self._leg(res, pickup_date=date(2026, 6, 8))

        self.assertEqual(daytime.afterhours_fee_outstanding(), Decimal("0.00"))
        self.assertEqual(late.afterhours_fee_outstanding(), Decimal("0.00"))

    def test_a_cancelled_late_leg_does_not_eat_the_guests_charge(self):
        """A cancelled leg still owes nothing, so it must not raise the bar.

        Reservation 14369: leg 24847 ran at 23:02, leg 24848 was cancelled, and
        the booking carries one $20 charge. Counting both late legs demanded
        $40, so the leg that actually ran kept reporting owed while the guest
        had paid for it.
        """
        res = self._res(additional=AFTERHOURS_FEE_AMOUNT)
        ran = self._leg(res)
        self._leg(res, pickup_date=date(2026, 6, 8), status="cancelled")

        self.assertEqual(ran.afterhours_fee_outstanding(), Decimal("0.00"))

    def test_marker_still_settles_it_on_its_own(self):
        """Charging the fee later stamps the marker; that must keep working."""
        res = self._res(additional=Decimal("0.00"))
        leg = self._leg(res, afterhours_fee=AFTERHOURS_FEE_AMOUNT)

        self.assertEqual(leg.afterhours_fee_outstanding(), Decimal("0.00"))

    def test_daytime_leg_is_never_owed(self):
        res = self._res(additional=Decimal("0.00"))
        leg = self._leg(res, pickup_time=time(14, 0))

        self.assertEqual(leg.afterhours_fee_outstanding(), Decimal("0.00"))


class NoTaskRaisedForAnAlreadyPaidFee(_Fixture, TestCase):
    """flag_afterhours_fee must stay silent when the booking already carries it."""

    def test_no_task_when_the_fee_is_itemised(self):
        from ops.models import OperationalTask
        from ops.tasks import flag_afterhours_fee

        res = self._res(additional=AFTERHOURS_FEE_AMOUNT)
        leg = self._leg(res)

        task = flag_afterhours_fee(leg, time(23, 30))

        self.assertIsNone(task)
        self.assertFalse(
            OperationalTask.objects.filter(
                leg=leg, task_type=OperationalTask.TaskType.AFTERHOURS_FEE
            ).exists()
        )

    def test_task_still_raised_when_the_fee_is_genuinely_unbilled(self):
        from ops.models import OperationalTask
        from ops.tasks import flag_afterhours_fee

        res = self._res(additional=Decimal("0.00"))
        leg = self._leg(res)

        task = flag_afterhours_fee(leg, time(23, 30))

        self.assertIsNotNone(task)
        self.assertEqual(task.task_type, OperationalTask.TaskType.AFTERHOURS_FEE)


class BookingRecordsWhetherTheFeeIsInThePrice(TestCase):
    """
    Ask the person who knows, at the moment they know it.

    The dispatcher booking screen already warns that the $20 "is not in this
    price yet" when the extras don't cover it — but it is only a warning, and
    nothing recorded what the dispatcher decided. So a fee folded into a manual
    quote looked identical to one nobody charged, and the difference surfaced
    days later as a task aimed at someone who could no longer tell.

    `direct` bookings are 85 of the 123 late trips in the last 60 days that
    would still flag, which is exactly this case.
    """

    def _decide(self, **kw):
        from reservations.utils import afterhours_marker_at_booking
        args = dict(
            pickup_time=time(23, 30),
            fee_included=None,
            additional_charges=Decimal("0.00"),
            afterhours_total=AFTERHOURS_FEE_AMOUNT,
        )
        args.update(kw)
        return afterhours_marker_at_booking(**args)

    def test_daytime_pickup_never_carries_the_fee(self):
        self.assertEqual(self._decide(pickup_time=time(14, 0)), Decimal("0.00"))

    def test_dispatcher_says_it_is_in_the_price(self):
        self.assertEqual(self._decide(fee_included=True), AFTERHOURS_FEE_AMOUNT)

    def test_dispatcher_says_it_is_not_being_charged(self):
        """Left unmarked on purpose, so it surfaces once for a decision."""
        self.assertEqual(self._decide(fee_included=False), Decimal("0.00"))

    def test_itemised_charges_still_count_when_nobody_answered(self):
        """Unanswered (an older in-flight booking) falls back to the money."""
        self.assertEqual(
            self._decide(fee_included=None, additional_charges=AFTERHOURS_FEE_AMOUNT),
            AFTERHOURS_FEE_AMOUNT,
        )

    def test_no_answer_and_no_money_leaves_it_unmarked(self):
        self.assertEqual(self._decide(fee_included=None), Decimal("0.00"))

    def test_an_explicit_no_beats_incidental_charges(self):
        """$20 of car seats is not the after-hours fee if the dispatcher says so."""
        self.assertEqual(
            self._decide(fee_included=False, additional_charges=AFTERHOURS_FEE_AMOUNT),
            Decimal("0.00"),
        )

    def test_two_late_legs_need_both_covered_to_infer_it(self):
        self.assertEqual(
            self._decide(
                fee_included=None,
                additional_charges=AFTERHOURS_FEE_AMOUNT,
                afterhours_total=AFTERHOURS_FEE_AMOUNT * 2,
            ),
            Decimal("0.00"),
        )


class BackfillOnlyStampsWhatItCanProve(_Fixture, TestCase):
    """
    The one-time cleanup for legs booked before the pricing question existed.

    It stamps a leg only when the arithmetic proves the money is there: the
    reservation's total equals the standard rate for its route plus a fee for
    every late leg on it. Anything it cannot prove is left alone and keeps
    flagging, because ~160 of these really were never billed and quietly marking
    those "collected" would write off money on trips that can still be charged.
    """

    def _run(self, **kw):
        from reservations.afterhours_backfill import provable_legs
        return provable_legs(**kw)

    def test_a_leg_whose_total_is_standard_plus_the_fee_is_stamped(self):
        res = self._res(base=Decimal("215.00"))  # round trip standard is 195
        leg = self._leg(res)

        self.assertIn(leg.id, [l.id for l in self._run()])

    def test_a_leg_paid_exactly_standard_is_left_alone(self):
        """The genuinely-unbilled case — this is money still worth collecting."""
        res = self._res(base=Decimal("195.00"))
        leg = self._leg(res)

        self.assertNotIn(leg.id, [l.id for l in self._run()])

    def test_a_leg_paid_more_than_standard_plus_fee_is_left_alone(self):
        """Extras muddy it — can't prove the $20 is the after-hours fee."""
        res = self._res(base=Decimal("300.00"))
        leg = self._leg(res)

        self.assertNotIn(leg.id, [l.id for l in self._run()])

    def test_a_cancelled_reservation_is_left_alone(self):
        res = self._res(base=Decimal("215.00"))
        leg = self._leg(res)
        res.status = "cancelled"
        res.save(update_fields=["status"])

        self.assertNotIn(leg.id, [l.id for l in self._run()])

    def test_two_late_legs_need_two_fees_above_standard(self):
        res = self._res(base=Decimal("215.00"))  # standard 195 + only ONE fee
        leg1 = self._leg(res)
        leg2 = self._leg(res, pickup_date=date(2026, 6, 8))
        ids = [l.id for l in self._run()]

        self.assertNotIn(leg1.id, ids)
        self.assertNotIn(leg2.id, ids)

    def test_two_late_legs_with_both_fees_are_stamped(self):
        res = self._res(base=Decimal("235.00"))  # standard 195 + two fees
        leg1 = self._leg(res)
        leg2 = self._leg(res, pickup_date=date(2026, 6, 8))
        ids = [l.id for l in self._run()]

        self.assertIn(leg1.id, ids)
        self.assertIn(leg2.id, ids)

    def test_an_already_marked_leg_is_not_touched_again(self):
        res = self._res(base=Decimal("215.00"))
        leg = self._leg(res, afterhours_fee=AFTERHOURS_FEE_AMOUNT)

        self.assertNotIn(leg.id, [l.id for l in self._run()])

    def test_a_daytime_leg_is_never_stamped(self):
        res = self._res(base=Decimal("215.00"))
        leg = self._leg(res, pickup_time=time(13, 0))

        self.assertNotIn(leg.id, [l.id for l in self._run()])

    def test_a_cancelled_leg_is_never_stamped(self):
        res = self._res(base=Decimal("215.00"))
        leg = self._leg(res, status="cancelled")

        self.assertNotIn(leg.id, [l.id for l in self._run()])

    def test_applying_it_marks_the_leg_and_says_why(self):
        from reservations.afterhours_backfill import apply_backfill

        res = self._res(base=Decimal("215.00"))
        leg = self._leg(res)

        count = apply_backfill()

        self.assertEqual(count, 1)
        leg.refresh_from_db()
        self.assertEqual(leg.afterhours_fee, AFTERHOURS_FEE_AMOUNT)
        self.assertIn("standard rate", leg.private_notes.lower())

    def test_running_it_twice_changes_nothing_the_second_time(self):
        from reservations.afterhours_backfill import apply_backfill

        res = self._res(base=Decimal("215.00"))
        self._leg(res)

        self.assertEqual(apply_backfill(), 1)
        self.assertEqual(apply_backfill(), 0)


class ThePricingScreenAsksTheQuestion(TestCase):
    """The radio only appears when there is a late leg to ask about."""

    def _render(self, afterhours_legs):
        from django.template.loader import render_to_string

        from dispatching.forms import DispatcherPricingForm

        return render_to_string(
            "dispatching/booking/step_pricing.html",
            {
                "form": DispatcherPricingForm(),
                "afterhours_legs": afterhours_legs,
                "afterhours_fee": AFTERHOURS_FEE_AMOUNT,
                "legs_data": [{}],
                "suggested_price": None,
            },
        )

    def test_the_question_is_shown_for_a_late_pickup(self):
        html = self._render([1])

        self.assertIn('id="rw-afterhours-ask"', html)
        self.assertIn("the $20 is in this price", html)
        self.assertIn("not charging it", html)
        self.assertIn('name="afterhours_fee_included"', html)

    def test_no_question_when_nothing_is_late(self):
        html = self._render([])

        self.assertNotIn('id="rw-afterhours-ask"', html)


class SettlingTheFeeStopsItComingBack(_Fixture, TestCase):
    """
    The collected-some-other-way case.

    A dispatcher can take the $20 in ways the booking never records: on a
    bundled balance payment ("Paying remaining amount + After hours fee"), in
    cash, or folded into a manual price. Closing the task did not record any of
    that — ``close_task`` only writes a status — so the next flight refresh
    raised the same task again. Leg 23272 collected three that way, two of them
    closed by different dispatchers who each correctly declined to charge.

    Settling must therefore write the marker, not just close the task.
    """

    def _open_task(self, leg):
        from ops.models import OperationalTask
        return OperationalTask.objects.filter(
            leg=leg,
            task_type=OperationalTask.TaskType.AFTERHOURS_FEE,
            status__in=list(OperationalTask.OPEN_STATUSES),
        )

    def test_settling_marks_the_fee_and_closes_the_open_task(self):
        from ops.tasks import flag_afterhours_fee, settle_afterhours_fee

        res = self._res(additional=Decimal("0.00"))
        leg = self._leg(res)
        flag_afterhours_fee(leg, time(23, 30))
        self.assertEqual(self._open_task(leg).count(), 1)

        settle_afterhours_fee(leg, note="Collected on the balance payment")

        leg.refresh_from_db()
        self.assertEqual(leg.afterhours_fee, AFTERHOURS_FEE_AMOUNT)
        self.assertEqual(self._open_task(leg).count(), 0)

    def test_a_settled_fee_is_no_longer_outstanding(self):
        from ops.tasks import settle_afterhours_fee

        res = self._res(additional=Decimal("0.00"))
        leg = self._leg(res)

        settle_afterhours_fee(leg, note="Taken in cash")

        leg.refresh_from_db()
        self.assertEqual(leg.afterhours_fee_outstanding(), Decimal("0.00"))

    def test_a_settled_fee_does_not_come_back_on_the_next_refresh(self):
        """The whole point — the next scan must stay silent."""
        from ops.tasks import flag_afterhours_fee, settle_afterhours_fee

        res = self._res(additional=Decimal("0.00"))
        leg = self._leg(res)
        settle_afterhours_fee(leg, note="Taken in cash")
        leg.refresh_from_db()

        task = flag_afterhours_fee(leg, time(23, 30))

        self.assertIsNone(task)
        self.assertEqual(self._open_task(leg).count(), 0)

    def test_dispatcher_can_settle_it_from_the_page(self):
        """The button a dispatcher actually presses."""
        from django.contrib.auth.models import User
        from django.urls import reverse

        from ops.tasks import flag_afterhours_fee

        res = self._res(additional=Decimal("0.00"))
        leg = self._leg(res)
        flag_afterhours_fee(leg, time(23, 30))
        staff = User.objects.create_user("dispatch1", password="x", is_staff=True)
        self.client.force_login(staff)

        resp = self.client.post(
            reverse("settle_afterhours_fee", kwargs={"leg_id": leg.id}),
            {"note": "Guest paid it with the balance"},
        )

        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["success"])
        leg.refresh_from_db()
        self.assertEqual(leg.afterhours_fee, AFTERHOURS_FEE_AMOUNT)
        self.assertEqual(self._open_task(leg).count(), 0)

    def test_a_non_staff_user_cannot_settle_a_fee(self):
        from django.contrib.auth.models import User
        from django.urls import reverse

        res = self._res(additional=Decimal("0.00"))
        leg = self._leg(res)
        outsider = User.objects.create_user("guest1", password="x", is_staff=False)
        self.client.force_login(outsider)

        resp = self.client.post(
            reverse("settle_afterhours_fee", kwargs={"leg_id": leg.id})
        )

        self.assertEqual(resp.status_code, 403)
        leg.refresh_from_db()
        self.assertEqual(leg.afterhours_fee, Decimal("0.00"))

    def test_settling_records_who_and_why_on_the_leg(self):
        """A money field changing needs an audit trail someone can read back."""
        from ops.tasks import settle_afterhours_fee

        res = self._res(additional=Decimal("0.00"))
        leg = self._leg(res)

        settle_afterhours_fee(leg, note="Collected on the balance payment")

        leg.refresh_from_db()
        self.assertIn("Collected on the balance payment", leg.private_notes)
