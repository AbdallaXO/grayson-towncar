"""Advisor ledger tests — the log that keeps the precision number true (Phase 1.2).

Run with:  ENABLE_DEBUG_TOOLBAR=0 ./manage.py test dispatching.tests_advisor_events

What is pinned here and why:

  * THE OUTCOME RULE IS 23'S, EXACTLY. ``leg_lateness`` has one job: reproduce
    ``analysis/23_advisor_replay.build_truth`` so a live precision number can be
    compared with the replay's. The three ways it can silently drift are pinned
    as their own cases — the LAST on-location tap (LegStatus.Meta.ordering is
    ``["-timestamp"]``, and production's own first_status_times returns the
    earliest), the batch-tap rule, and NO status filter (build_truth scores
    cancelled legs too). ``analysis/27_advisor_event_gate.py --verify-fill`` is
    the same claim tested against 3,000 real legs; these are the unit form.
  * ONE ROW PER EPISODE. A card id is an anti-flap key, not a lifecycle: a card
    can leave the rail and come back hours later under the same string. A
    sighting inside EPISODE_GAP_MIN extends; one beyond it opens episode 2.
  * A CARD MUTATES UNDER A STABLE ID. severity/basis keep BOTH ends, and the
    impact leg (``leg_ids[-1]``, which is what the outcome grades) keeps both
    the first claim and the last.
  * THE LEDGER NEVER BREAKS THE BOARD. Recording is bounded work on a 60-second
    poll and every writer swallows its own failures — a rail that goes dark
    because a log row failed would be a far worse bug than a missing row.
  * THE FILL IS IDEMPOTENT BY DATA, NOT BY CADENCE. It rides a 30-minute loop
    and must be safe to run at any hour, any number of times.
"""
from datetime import date, time, timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from dispatching import advisor_events as ae
from dispatching.models import AdvisorEvent
from dispatching.scheduler import preload_timing_cache
from drivers.models import Driver, DriverVehicleAssignment, FleetVehicle
from rates.models import Location, Rate, Route, Vehicle
from reservations.models import Customer, Leg, LegStatus, Reservation

DAY = timezone.localdate() + timedelta(days=7)
PAST = timezone.localdate() - timedelta(days=2)


def _card(card_id="overlap:1:2", legs=(1, 2), severity="critical",
          basis="clock_only", kind="overlap", plans=(), **kw):
    """A serialized disruption card in the engine's contract shape."""
    c = {"id": card_id, "kind": kind, "severity": severity,
         "headline": "Sam's 9:00 and 9:05 overlap", "narrative": "",
         "impact_at": None, "leg_ids": list(legs), "task_id": None,
         "basis": basis, "plans": list(plans), "detected_only": False,
         "no_internal_solution": False}
    c.update(kw)
    return c


class _LedgerFixture(TestCase):
    @classmethod
    def setUpTestData(cls):
        preload_timing_cache()
        cls.vehicle = Vehicle.objects.create(
            vehicle_type="towncar", capacity=4, luggage_capacity=4)
        origin = Location.objects.create(name="MCO")
        dest = Location.objects.create(name="Disney")
        cls.route = Route.objects.create(
            origin=origin, destination=dest, inhouse_base_pay=Decimal("50.00"))
        cls.rate = Rate.objects.create(
            vehicle=cls.vehicle, route=cls.route,
            oneway_price=Decimal("100.00"), round_trip_price=Decimal("180.00"))
        cls.customer = Customer.objects.create(
            first_name="John", last_name="Doe", email="john@example.com",
            phone_number="5551234567")
        cls.sam = Driver.objects.create(
            profile=User.objects.create_user("le_sam", first_name="Sam"),
            driver_type="inhouse")
        fleet = FleetVehicle.objects.create(
            vehicle_number="LE-1", vehicle_type=cls.vehicle, year=2024,
            make="Lincoln", model="Continental")
        DriverVehicleAssignment.objects.create(driver=cls.sam, date=DAY, vehicle=fleet)
        cls.staff = User.objects.create_user(
            "le_staff", password="x", is_staff=True, is_superuser=True)

    def setUp(self):
        cache.clear()

    def _leg(self, pickup_time=time(9, 0), day=None, **kw):
        res = Reservation.objects.create(
            trip_type="one-way", customer=self.customer, rate=self.rate,
            vehicle=self.vehicle, base_price=Decimal("100.00"),
            total_price=Decimal("100.00"))
        defaults = dict(
            reservation=res, pickup_date=day or DAY, pickup_time=pickup_time,
            pickup_location="MCO", dropoff_location="Disney", route=self.route,
            status="confirmed")
        defaults.update(kw)
        return Leg.objects.create(**defaults)

    @staticmethod
    def _tap(leg, status, when):
        """A status tap at a naive LOCAL wall-clock time."""
        return LegStatus.objects.create(
            leg=leg, status=status,
            timestamp=timezone.make_aware(when, timezone.get_current_timezone()))


# ══════════════════════════════════════════════════════════════════════════
# The outcome rule — 23's build_truth, reproduced
# ══════════════════════════════════════════════════════════════════════════

class LegLatenessTests(_LedgerFixture):
    def _at(self, leg, hh, mm, day=None):
        from datetime import datetime
        d = day or leg.pickup_date
        return datetime.combine(d, time(hh, mm))

    def test_on_time_tap_is_scored_not_late(self):
        leg = self._leg(time(9, 0))
        self._tap(leg, "on-location", self._at(leg, 8, 55))
        o = ae.leg_lateness([leg.id])[leg.id]
        self.assertEqual(o.quality, "ok")
        self.assertEqual(o.late_min, -5.0)
        self.assertEqual(o.basis, "booked pickup")

    def test_late_minutes_carry_one_decimal_like_the_replay(self):
        """23 rounds to ONE DECIMAL and compares strictly (>15), so 15.0 is not
        late and 15.1 is. An int here would silently move the D5 bar."""
        from datetime import datetime
        leg = self._leg(time(9, 0))
        self._tap(leg, "on-location",
                  datetime.combine(leg.pickup_date, time(9, 15)) +
                  timedelta(seconds=6))
        o = ae.leg_lateness([leg.id])[leg.id]
        self.assertEqual(o.late_min, 15.1)
        self.assertGreater(o.late_min, 15)

    def test_the_LAST_on_location_tap_wins_not_the_first(self):
        """LegStatus.Meta.ordering is ["-timestamp"] and production's own
        analytics.first_status_times returns the EARLIEST tap. 23 reads
        rows[-1] off an ascending scan. Measured cost of picking the wrong one
        on real data: the >15 verdict flips on 10 legs in 3,108."""
        leg = self._leg(time(9, 0))
        self._tap(leg, "on-location", self._at(leg, 8, 50))
        self._tap(leg, "on-location", self._at(leg, 9, 40))
        o = ae.leg_lateness([leg.id])[leg.id]
        self.assertEqual(o.late_min, 40.0)

    def test_no_tap_at_all_is_none_and_never_scored(self):
        leg = self._leg(time(9, 0))
        o = ae.leg_lateness([leg.id])[leg.id]
        self.assertEqual(o.quality, "none")
        self.assertIsNone(o.late_min)

    def test_batch_entered_day_is_discarded(self):
        """19's rule: a driver clearing a whole day in one go taps picked-up and
        completed within 120 s and never taps on-location. That is bookkeeping,
        not a clock, so it is thrown away rather than counted as on time."""
        leg = self._leg(time(9, 0))
        self._tap(leg, "picked-up", self._at(leg, 22, 0))
        self._tap(leg, "completed", self._at(leg, 22, 1))
        o = ae.leg_lateness([leg.id])[leg.id]
        self.assertEqual(o.quality, "batch")
        self.assertIsNone(o.late_min)

    def test_picked_up_and_completed_far_apart_is_none_not_batch(self):
        leg = self._leg(time(9, 0))
        self._tap(leg, "picked-up", self._at(leg, 9, 5))
        self._tap(leg, "completed", self._at(leg, 9, 55))
        self.assertEqual(ae.leg_lateness([leg.id])[leg.id].quality, "none")

    def test_cancelled_legs_are_still_scored(self):
        """build_truth applies NO status filter — 85 of the 3,108 legs on the
        replay dates are cancelled and still get a truth row. Excluding them
        here would make the live number incomparable with the replay's, which
        is the only reason this table exists."""
        leg = self._leg(time(9, 0), status="cancelled")
        self._tap(leg, "on-location", self._at(leg, 9, 30))
        o = ae.leg_lateness([leg.id])[leg.id]
        self.assertEqual(o.quality, "ok")
        self.assertEqual(o.late_min, 30.0)

    def test_a_leg_that_does_not_exist_is_simply_absent(self):
        self.assertEqual(ae.leg_lateness([424242]), {})

    def test_empty_and_falsy_ids_query_nothing(self):
        with self.assertNumQueries(0):
            self.assertEqual(ae.leg_lateness([]), {})
            self.assertEqual(ae.leg_lateness([None, 0]), {})

    def test_many_legs_cost_a_bounded_number_of_queries(self):
        """The nightly fill reads a fortnight of impact legs in one call. Two
        queries, not two per leg: dropping the legflight prefetch or the
        flight_information select_related turns pickup_deadline into an N+1
        that produces the RIGHT answers slowly, so nothing else would catch it."""
        from django.db import connection
        from django.test.utils import CaptureQueriesContext
        legs = [self._leg(time(9, 0)) for _ in range(12)]
        for leg in legs:
            self._tap(leg, "on-location", self._at(leg, 9, 5))
        with CaptureQueriesContext(connection) as ctx:
            out = ae.leg_lateness([l.id for l in legs])
        self.assertEqual(len(out), 12)
        # taps + legs(+reservation/flight_information joins) + legflight
        # + the flights behind it. Four, for twelve legs or twelve hundred.
        self.assertLessEqual(len(ctx.captured_queries), 4,
                             "\n".join(q["sql"][:120]
                                       for q in ctx.captured_queries))


# ══════════════════════════════════════════════════════════════════════════
# Recording — one row per episode
# ══════════════════════════════════════════════════════════════════════════

class RecordCardsTests(_LedgerFixture):
    def test_first_sighting_opens_episode_one(self):
        ae.record_cards(DAY, [_card()], source="rail")
        row = AdvisorEvent.objects.get()
        self.assertEqual((row.card_id, row.episode, row.kind), ("overlap:1:2", 1, "overlap"))
        self.assertEqual((row.severity, row.basis), ("critical", "clock_only"))
        self.assertEqual((row.impact_leg_id, row.impact_leg_first_id), (2, 2))
        self.assertEqual((row.leg_count, row.sightings, row.source), (2, 1, "rail"))

    def test_a_second_sighting_extends_the_same_episode(self):
        ae.record_cards(DAY, [_card()])
        ae.record_cards(DAY, [_card()])
        row = AdvisorEvent.objects.get()
        self.assertEqual(row.sightings, 2)
        self.assertEqual(AdvisorEvent.objects.count(), 1)

    def test_a_sighting_past_the_gap_opens_a_new_episode(self):
        """The id is an anti-flap key, not a lifecycle: overlap:{prev}:{next}
        is reborn under the same string every time the same pair breaks again,
        and a fresh on-time GPS ping can take a card off the rail for an hour.

        The threshold is measured, not chosen: 98 real boundaries over the 28
        replayed days sit at min 6 min / P50 28.5 / P75 72, so a gap of 45 —
        this file's first draft — would have merged two thirds of them."""
        long_ago = timezone.now() - timedelta(minutes=ae.EPISODE_GAP_MIN + 5)
        ae.record_cards(DAY, [_card()], seen_at=long_ago)
        ae.record_cards(DAY, [_card()])
        self.assertEqual(
            list(AdvisorEvent.objects.order_by("episode")
                 .values_list("episode", flat=True)), [1, 2])

    def test_severity_and_basis_keep_both_ends(self):
        """basis flips under a stable id the moment a picked-up tap lands, and
        severity can harden. A single value would report whichever end the log
        happened to catch."""
        ae.record_cards(DAY, [_card(severity="warning", basis="clock_only")])
        ae.record_cards(DAY, [_card(severity="critical", basis="recorded_pickup")])
        row = AdvisorEvent.objects.get()
        self.assertEqual((row.severity, row.basis), ("warning", "clock_only"))
        self.assertEqual((row.severity_last, row.basis_last),
                         ("critical", "recorded_pickup"))

    def test_a_moved_impact_leg_is_visible_not_overwritten(self):
        """leg_ids[-1] is what the outcome grades, and it moves as the cascade
        re-walks the chain — measured on 7.7% of episodes in the replay. The
        row grades the last claim and keeps the first, so a moved claim shows."""
        ae.record_cards(DAY, [_card(legs=(1,))])
        ae.record_cards(DAY, [_card(legs=(1, 5, 9))])
        row = AdvisorEvent.objects.get()
        self.assertEqual((row.impact_leg_first_id, row.impact_leg_id), (1, 9))
        self.assertEqual(row.leg_count, 3)

    def test_had_plans_is_sticky_across_sightings(self):
        ae.record_cards(DAY, [_card(plans=[{"id": "p1"}])])
        ae.record_cards(DAY, [_card(plans=[])])
        self.assertTrue(AdvisorEvent.objects.get().had_plans)

    def test_the_same_id_on_two_dates_is_two_rows(self):
        """The engine's id carries no date — 'unassigned:5501' is the same
        string tomorrow."""
        ae.record_cards(DAY, [_card()])
        ae.record_cards(DAY + timedelta(days=1), [_card()])
        self.assertEqual(AdvisorEvent.objects.count(), 2)

    def test_synthetic_farm_pending_cards_are_recorded_too(self):
        """They are snoozable through the same endpoint, so the ledger has to
        be able to resolve their ids. Their own kind keeps them out of every
        per-class precision readout."""
        ae.record_cards(DAY, [_card(card_id="farm_pending:77", kind="farm_pending",
                                    legs=(77,), severity="watch", basis="")])
        self.assertEqual(AdvisorEvent.objects.get().kind, "farm_pending")

    def test_recording_is_a_bounded_number_of_queries_whatever_the_card_count(self):
        """This sits on a 60-second poll. One SELECT plus at most two writes,
        for one card or twenty."""
        cards = [_card(card_id=f"overlap:{i}:{i + 1}", legs=(i, i + 1))
                 for i in range(20)]
        with self.assertNumQueries(2):
            ae.record_cards(DAY, cards)
        self.assertEqual(AdvisorEvent.objects.count(), 20)
        with self.assertNumQueries(2):
            ae.record_cards(DAY, cards)

    def test_garbage_cards_never_raise(self):
        self.assertEqual(ae.record_cards(DAY, []), 0)
        self.assertEqual(ae.record_cards(DAY, [{}]), 0)
        ae.record_cards(DAY, [{"id": "x:1", "leg_ids": None}])
        self.assertEqual(AdvisorEvent.objects.get().card_id, "x:1")

    def test_a_database_failure_is_swallowed(self):
        with patch.object(AdvisorEvent.objects, "bulk_create",
                          side_effect=RuntimeError("boom")):
            self.assertEqual(ae.record_cards(DAY, [_card()]), 0)


class StampTests(_LedgerFixture):
    def test_applied_stamps_the_open_episode(self):
        ae.record_cards(DAY, [_card()])
        ae.record_applied(DAY, "overlap:1:2", plan_id="overlap:1:2#p1",
                          user=self.staff, mode="live", snapshot_id=7)
        row = AdvisorEvent.objects.get()
        self.assertIsNotNone(row.applied_at)
        self.assertEqual(row.applied_by_id, self.staff.id)
        self.assertEqual((row.applied_plan_id, row.applied_mode,
                          row.applied_snapshot_id),
                         ("overlap:1:2#p1", "live", 7))

    def test_an_apply_from_a_surface_that_never_logged_still_lands(self):
        """The ops task-detail page can apply a plan. An apply is the most
        important event on the row, so it mints one rather than being lost —
        with a blank severity, because no write path carries it."""
        ae.record_applied(DAY, "overlap:1:2", user=self.staff)
        row = AdvisorEvent.objects.get()
        self.assertEqual((row.kind, row.severity, row.sightings, row.source),
                         ("overlap", "", 0, "task"))

    def test_a_rejected_apply_never_mints_a_row(self):
        """A refusal against a card nobody logged would be an event with no
        card behind it — no class to group by, so nothing a readout could use."""
        ae.record_rejected(DAY, "overlap:1:2", status=409, error="Board changed")
        self.assertEqual(AdvisorEvent.objects.count(), 0)
        ae.record_cards(DAY, [_card()])
        ae.record_rejected(DAY, "overlap:1:2", status=409, error="Board changed")
        row = AdvisorEvent.objects.get()
        self.assertEqual(row.rejected_status, 409)
        self.assertIsNone(row.applied_at)

    def test_re_snoozing_counts_rather_than_erroring(self):
        ae.record_cards(DAY, [_card()])
        ae.record_snoozed(DAY, "overlap:1:2", minutes=30, user=self.staff)
        ae.record_snoozed(DAY, "overlap:1:2", minutes=240, user=self.staff)
        row = AdvisorEvent.objects.get()
        self.assertEqual((row.snooze_count, row.snoozed_minutes), (2, 240))

    def test_a_dedup_hit_is_recorded_as_not_created(self):
        """create_task returning None means the 30-minute scanner had already
        filed the same task. Counting that as a filing would overstate the rail."""
        ae.record_cards(DAY, [_card()])
        ae.record_task_filed(DAY, "overlap:1:2", task_id=12, created=False,
                             user=self.staff)
        row = AdvisorEvent.objects.get()
        self.assertEqual((row.task_id, row.task_created), (12, False))

    def test_a_blank_card_id_is_a_no_op(self):
        """parse_advisor_plan accepts an empty disruption_id — it never
        validates one — so every stamp has to survive it."""
        self.assertIsNone(ae.record_applied(DAY, "", user=self.staff))
        self.assertEqual(AdvisorEvent.objects.count(), 0)


# ══════════════════════════════════════════════════════════════════════════
# The nightly fill
# ══════════════════════════════════════════════════════════════════════════

class FillOutcomesTests(_LedgerFixture):
    def _closed_row(self, leg_id, day=PAST, card_id="overlap:1:2"):
        return AdvisorEvent.objects.create(
            service_date=day, card_id=card_id, kind="overlap",
            severity="critical", severity_last="critical",
            impact_leg_id=leg_id, impact_leg_first_id=leg_id, leg_count=2,
            first_seen_at=timezone.now(), last_seen_at=timezone.now())

    def test_a_closed_day_is_graded(self):
        leg = self._leg(time(9, 0), day=PAST)
        self._tap(leg, "on-location",
                  timezone.datetime(PAST.year, PAST.month, PAST.day, 9, 25))
        row = self._closed_row(leg.id)
        self.assertEqual(ae.fill_outcomes(), {"graded": 1, "scored": 1})
        row.refresh_from_db()
        self.assertEqual(row.outcome_quality, "ok")
        self.assertEqual(row.outcome_late_min, 25.0)
        self.assertEqual(row.outcome_deadline_basis, "booked pickup")
        self.assertIsNotNone(row.outcome_filled_at)
        self.assertEqual(row.outcome_attempts, 1)

    def test_a_day_still_running_is_left_alone(self):
        """Grading a day before it ends would score trips that have not
        happened yet as 'no tap'."""
        leg = self._leg(time(9, 0), day=DAY)
        self._closed_row(leg.id, day=DAY)
        self.assertEqual(ae.fill_outcomes()["graded"], 0)

    def test_running_twice_grades_nothing_the_second_time(self):
        """It rides a 30-minute loop, so it must be safe at any hour and any
        number of times — idempotent by data, not by a cadence gate."""
        leg = self._leg(time(9, 0), day=PAST)
        self._tap(leg, "on-location",
                  timezone.datetime(PAST.year, PAST.month, PAST.day, 9, 5))
        self._closed_row(leg.id)
        ae.fill_outcomes()
        self.assertEqual(ae.fill_outcomes()["graded"], 0)

    def test_an_untapped_leg_is_retried_and_a_batch_one_is_not(self):
        """Only 'none' can still become 'ok' — taps land days late. 'batch',
        'no_deadline' and 'unknown' never change, so asking again is waste."""
        untapped = self._leg(time(9, 0), day=PAST)
        batched = self._leg(time(10, 0), day=PAST)
        self._tap(batched, "picked-up",
                  timezone.datetime(PAST.year, PAST.month, PAST.day, 22, 0))
        self._tap(batched, "completed",
                  timezone.datetime(PAST.year, PAST.month, PAST.day, 22, 1))
        a = self._closed_row(untapped.id, card_id="overlap:1:2")
        b = self._closed_row(batched.id, card_id="overlap:3:4")
        ae.fill_outcomes()
        a.refresh_from_db(); b.refresh_from_db()
        self.assertEqual((a.outcome_quality, b.outcome_quality), ("none", "batch"))
        later = timezone.now() + timedelta(
            hours=ae.OUTCOME_RETRY_EVERY_HOURS, minutes=1)
        self.assertEqual(ae.fill_outcomes(now=later)["graded"], 1)
        a.refresh_from_db()
        self.assertEqual(a.outcome_attempts, 2)

    def test_a_retry_stops_after_the_attempt_cap(self):
        leg = self._leg(time(9, 0), day=PAST)
        row = self._closed_row(leg.id)
        row.outcome_quality = "none"
        row.outcome_attempts = ae.OUTCOME_MAX_ATTEMPTS
        row.outcome_filled_at = timezone.now()
        row.save()
        self.assertEqual(ae.fill_outcomes()["graded"], 0)

    def test_an_impact_leg_that_is_gone_is_unknown_not_dropped(self):
        """23's scorer counts unscorable cards rather than dropping them; a
        denominator that quietly loses them would overstate the tool."""
        row = self._closed_row(999999)
        ae.fill_outcomes()
        row.refresh_from_db()
        self.assertEqual(row.outcome_quality, "unknown")
        self.assertIsNotNone(row.outcome_filled_at)

    def test_a_row_with_no_impact_leg_is_still_closed_out(self):
        row = self._closed_row(None)
        ae.fill_outcomes()
        row.refresh_from_db()
        self.assertEqual(row.outcome_quality, "unknown")

    def test_a_failure_is_swallowed_so_the_loop_survives(self):
        leg = self._leg(time(9, 0), day=PAST)
        self._closed_row(leg.id)
        with patch.object(ae, "leg_lateness", side_effect=RuntimeError("boom")):
            self.assertEqual(ae.fill_outcomes(), {"graded": 0, "scored": 0})


# ══════════════════════════════════════════════════════════════════════════
# The endpoint, end to end
# ══════════════════════════════════════════════════════════════════════════

class EndpointLedgerTests(_LedgerFixture):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.staff)

    def _state(self, **params):
        params.setdefault("date", DAY.isoformat())
        return self.client.get(reverse("recovery_advisor_state"), params)

    def test_a_served_card_is_written_down(self):
        state = {"fingerprint": "fp", "computed_at": f"{DAY.isoformat()}T10:00",
                 "truncated": False, "disruptions": [_card()]}
        with patch("dispatching.conflict_advisor.compute_advisor_state",
                   return_value=state):
            self.assertEqual(self._state().status_code, 200)
        row = AdvisorEvent.objects.get()
        self.assertEqual((row.card_id, row.source), ("overlap:1:2", "rail"))

    def test_a_snoozed_card_is_not_recorded_as_shown_again(self):
        """The row records what was SENT. A snoozed card is filtered out before
        the response is built, so it stops accruing sightings — while the
        snooze stamp itself lands at the moment of the POST, because the snooze
        list is cache-only and gone within four hours."""
        state = {"fingerprint": "fp", "computed_at": f"{DAY.isoformat()}T10:00",
                 "truncated": False, "disruptions": [_card()]}
        with patch("dispatching.conflict_advisor.compute_advisor_state",
                   return_value=state):
            self._state()
            self.client.post(
                reverse("recovery_advisor_snooze"),
                {"date": DAY.isoformat(), "disruption_id": "overlap:1:2"},
                content_type="application/json")
            self._state()
        row = AdvisorEvent.objects.get()
        self.assertEqual(row.sightings, 1)
        self.assertEqual(row.snooze_count, 1)
        self.assertEqual(row.snoozed_by_id, self.staff.id)

    def test_the_short_circuit_still_writes_nothing_and_stays_in_budget(self):
        """The unchanged-fingerprint branch is contractually three queries
        (tests_recovery_advisor.EndpointQueryBudgetTests). The ledger lives on
        the changed branch precisely so that stays true."""
        state = {"fingerprint": "fp", "computed_at": f"{DAY.isoformat()}T10:00",
                 "truncated": False, "disruptions": [_card()]}
        with patch("dispatching.conflict_advisor.compute_advisor_state",
                   return_value=state):
            fp = self._state().json()["fingerprint"]
            AdvisorEvent.objects.all().delete()
            resp = self._state(fp=fp)
        self.assertFalse(resp.json()["changed"])
        self.assertEqual(AdvisorEvent.objects.count(), 0)

    def test_the_rail_survives_a_broken_ledger(self):
        """A rail that goes dark because a log row failed would be a far worse
        bug than a missing row."""
        state = {"fingerprint": "fp", "computed_at": f"{DAY.isoformat()}T10:00",
                 "truncated": False, "disruptions": [_card()]}
        with patch("dispatching.conflict_advisor.compute_advisor_state",
                   return_value=state), \
             patch("dispatching.advisor_events.record_cards",
                   side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                self._state()
        # The recorder's own guard is what makes this safe in production; the
        # patch above bypasses it, so assert the guard itself here.
        with patch.object(AdvisorEvent.objects, "bulk_create",
                          side_effect=RuntimeError("boom")), \
             patch("dispatching.conflict_advisor.compute_advisor_state",
                   return_value=state):
            self.assertEqual(self._state().status_code, 200)


# ══════════════════════════════════════════════════════════════════════════
# The apply path, end to end
# ══════════════════════════════════════════════════════════════════════════

from dispatching.tests_conflict_advisor_apply import (  # noqa: E402
    FUTURE, _AdvisorApplyFixture)


class ApplyLedgerTests(_AdvisorApplyFixture):
    """The stamp has to ride the apply's own transaction: an "applied" row for
    a move that rolled back would be worse than no row at all, because it is
    the ledger D14 would trust before letting anything apply itself."""

    def setUp(self):
        cache.clear()
        AdvisorEvent.objects.create(
            service_date=FUTURE, card_id="overlap:1:2", kind="overlap",
            severity="critical", severity_last="critical", leg_count=2,
            first_seen_at=timezone.now(), last_seen_at=timezone.now())

    def test_a_successful_apply_stamps_the_card(self):
        leg = self._leg()
        status, _body = self._apply(self._payload(
            [{"op": "reassign", "leg_id": leg.id, "to_driver_id": self.sam.id}],
            {leg.id: None}))
        self.assertEqual(status, 200)
        row = AdvisorEvent.objects.get(card_id="overlap:1:2")
        self.assertIsNotNone(row.applied_at)
        self.assertEqual(row.applied_plan_id, "overlap:1:2#p1")
        self.assertEqual(row.applied_mode, "live")
        self.assertEqual(row.applied_by_id, self.staff.id)
        self.assertIsNone(row.rejected_at)

    def test_a_rejected_apply_stamps_the_refusal_and_never_the_apply(self):
        """A 409 says the board moved under a dispatcher who WANTED the plan.
        That is not the same as nobody wanting it, and the row must not claim
        a move that never happened."""
        leg = self._leg(driver=self.sam)
        status, _body = self._apply(self._payload(
            [{"op": "reassign", "leg_id": leg.id, "to_driver_id": self.bob.id}],
            {leg.id: None}))          # expected says unassigned; it is not
        self.assertEqual(status, 409)
        row = AdvisorEvent.objects.get(card_id="overlap:1:2")
        self.assertIsNone(row.applied_at)
        self.assertEqual(row.rejected_status, 409)

    def test_a_malformed_payload_logs_nothing_and_still_400s(self):
        """parse_advisor_plan raises before there is a parsed day or card id —
        the one case where the handler has nothing to write."""
        status, _body = self._apply({"schema": 99})
        self.assertEqual(status, 400)
        row = AdvisorEvent.objects.get(card_id="overlap:1:2")
        self.assertIsNone(row.rejected_at)

    def test_a_broken_ledger_never_fails_a_good_apply(self):
        """The stamp sits inside the apply's atomic block, where a raise would
        be caught by the blanket handler and turned into 'Apply failed —
        nothing was changed'. It takes its own savepoint so it cannot."""
        leg = self._leg()
        with patch("dispatching.advisor_events._open_episode",
                   side_effect=RuntimeError("boom")):
            status, _body = self._apply(self._payload(
                [{"op": "reassign", "leg_id": leg.id, "to_driver_id": self.sam.id}],
                {leg.id: None}))
        self.assertEqual(status, 200)
        leg.refresh_from_db()
        self.assertEqual(leg.driver_id, self.sam.id)


class SweepTests(_LedgerFixture):
    """The unattended feed. Without it the ledger holds only what a superuser
    had on screen, which is not what either the §3.3(b) month of logging or
    Phase 2's two-week gate needs."""

    def _state(self, cards):
        return {"fingerprint": "fp", "computed_at": "x", "truncated": False,
                "disruptions": cards}

    def test_the_sweep_records_todays_cards_with_nobody_watching(self):
        with patch("dispatching.conflict_advisor.compute_advisor_state",
                   return_value=self._state([_card()])) as m:
            out = ae.sweep_today(now=timezone.make_aware(
                timezone.datetime(2026, 9, 5, 14, 0)))
        self.assertEqual(out, {"cards": 1})
        self.assertEqual(m.call_args[0][0], date(2026, 9, 5))
        row = AdvisorEvent.objects.get()
        self.assertEqual(row.source, "sweep")

    def test_the_sweep_is_silent_outside_the_operating_window(self):
        """23's replay window is 06:00-23:00, so the live and replayed
        populations have to be the same population — and outside it the board
        is empty and the compute is pure cost."""
        with patch("dispatching.conflict_advisor.compute_advisor_state") as m:
            ae.sweep_today(now=timezone.make_aware(
                timezone.datetime(2026, 9, 5, 3, 0)))
        m.assert_not_called()
        self.assertEqual(AdvisorEvent.objects.count(), 0)

    def test_the_constant_switches_it_off_completely(self):
        with patch.object(ae, "ADVISOR_EVENT_SWEEP", False), \
             patch("dispatching.conflict_advisor.compute_advisor_state") as m:
            self.assertEqual(ae.sweep_today(), {"cards": 0})
        m.assert_not_called()

    def test_repeated_sweeps_extend_one_episode_rather_than_piling_up(self):
        """It runs every 180 s. A card alive for two hours is one row with 40
        sightings, not 40 rows."""
        at = timezone.make_aware(timezone.datetime(2026, 9, 5, 14, 0))
        with patch("dispatching.conflict_advisor.compute_advisor_state",
                   return_value=self._state([_card()])):
            for i in range(5):
                ae.sweep_today(now=at + timedelta(minutes=3 * i))
        row = AdvisorEvent.objects.get()
        self.assertEqual(row.sightings, 5)

    def test_a_broken_engine_never_stops_the_samsara_poller(self):
        with patch("dispatching.conflict_advisor.compute_advisor_state",
                   side_effect=RuntimeError("boom")):
            self.assertEqual(ae.sweep_today(now=timezone.make_aware(
                timezone.datetime(2026, 9, 5, 14, 0))), {"cards": 0})


class ReviewFixTests(_LedgerFixture):
    """Four defects an adversarial pass found after the first commit. Each one
    made a number quietly wrong rather than raising anything, which is the
    failure mode this instrument exists to avoid."""

    def _closed_row(self, leg_id, day=PAST, card_id="overlap:1:2", **kw):
        f = dict(service_date=day, card_id=card_id, kind="overlap",
                 severity="critical", severity_last="critical",
                 impact_leg_id=leg_id, impact_leg_first_id=leg_id, leg_count=2,
                 first_seen_at=timezone.now(), last_seen_at=timezone.now())
        f.update(kw)
        return AdvisorEvent.objects.create(**f)

    def test_a_leg_that_moved_dates_is_unknown_not_scored(self):
        """A guest confirming which night an overnight arrival takes off moves
        the leg a day — and the advisor raises cards on exactly that
        population. Grading the leg's NEW date under the OLD service date would
        file a real, signed lateness for a trip that never ran then, on the
        false-positive side of the class D5 gates."""
        leg = self._leg(time(9, 0), day=PAST)
        row = self._closed_row(leg.id)
        leg.pickup_date = PAST + timedelta(days=1)
        leg.save(update_fields=["pickup_date"])
        self._tap(leg, "on-location",
                  timezone.datetime(leg.pickup_date.year, leg.pickup_date.month,
                                    leg.pickup_date.day, 8, 55))
        ae.fill_outcomes()
        row.refresh_from_db()
        self.assertEqual(row.outcome_quality, "unknown")
        self.assertIsNone(row.outcome_late_min)

    def test_an_unresolved_row_is_not_retried_on_the_same_loop_cycle(self):
        """The attempt cap is a runaway guard, not the schedule. Without
        spacing, the 30-minute loop spends all eight attempts four hours after
        the day closes and the seven-day window never binds at all."""
        leg = self._leg(time(9, 0), day=PAST)
        row = self._closed_row(leg.id)
        ae.fill_outcomes()
        row.refresh_from_db()
        self.assertEqual((row.outcome_quality, row.outcome_attempts), ("none", 1))
        self.assertEqual(ae.fill_outcomes()["graded"], 0)          # 30 min later
        later = timezone.now() + timedelta(
            hours=ae.OUTCOME_RETRY_EVERY_HOURS, minutes=1)
        self.assertEqual(ae.fill_outcomes(now=later)["graded"], 1)

    def test_a_late_tap_within_the_week_still_flips_the_row_to_scored(self):
        leg = self._leg(time(9, 0), day=PAST)
        row = self._closed_row(leg.id)
        ae.fill_outcomes()
        self._tap(leg, "on-location",
                  timezone.datetime(PAST.year, PAST.month, PAST.day, 9, 40))
        ae.fill_outcomes(now=timezone.now() + timedelta(
            hours=ae.OUTCOME_RETRY_EVERY_HOURS + 1))
        row.refresh_from_db()
        self.assertEqual(row.outcome_quality, "ok")
        self.assertEqual(row.outcome_late_min, 40.0)

    def test_a_leg_filtered_compute_never_claims_the_card_carried_plans(self):
        """for_leg_id narrows the card set BEFORE the six-card cap and the 4 s
        budget, so one surviving card always gets full plan generation. Letting
        that write had_plans would make the ops task page report plan coverage
        the whole-board replay never measured."""
        ae.record_cards(DAY, [_card(plans=[{"id": "p1"}])], whole_board=False)
        row = AdvisorEvent.objects.get()
        self.assertFalse(row.had_plans)
        ae.record_cards(DAY, [_card(plans=[{"id": "p1"}])])
        row.refresh_from_db()
        self.assertTrue(row.had_plans)          # a full-board sighting still sets it

    def test_a_racing_insert_recovers_the_row_instead_of_losing_the_stamp(self):
        """Two workers can open the same episode at once — three gunicorn
        workers share no cache, so they compute the same board independently.
        The unique constraint makes one lose, and the loser has to read back the
        winner. That read runs straight after a failed INSERT, which Django
        refuses on a transaction it has marked for rollback — so the INSERT
        needs its own savepoint or the applied/snoozed stamp is simply lost.

        The race is staged by making the FIRST lookup miss a row that is really
        there, which is exactly what the loser sees."""
        from django.db import transaction as djtx
        AdvisorEvent.objects.create(
            service_date=DAY, card_id="overlap:9:9", episode=1, kind="overlap",
            first_seen_at=timezone.now(), last_seen_at=timezone.now())
        real_filter = AdvisorEvent.objects.filter
        seen = {"n": 0}

        def blind_once(*a, **kw):
            seen["n"] += 1
            qs = real_filter(*a, **kw)
            return qs.none() if seen["n"] == 1 else qs

        with djtx.atomic():
            with patch.object(AdvisorEvent.objects, "filter",
                              side_effect=blind_once):
                row = ae._open_episode(DAY, "overlap:9:9",
                                       create_at=timezone.now())
            self.assertIsNotNone(row)             # recovered, not lost
            self.assertEqual(row.card_id, "overlap:9:9")
            # ...and the caller's transaction is still usable, which is the half
            # that fails without the savepoint.
            AdvisorEvent.objects.create(
                service_date=DAY, card_id="after:1", kind="x",
                first_seen_at=timezone.now(), last_seen_at=timezone.now())
        self.assertEqual(AdvisorEvent.objects.filter(card_id="after:1").count(), 1)
