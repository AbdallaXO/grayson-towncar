"""Chain repositioning uses the real road distance where the category table can't price it.

Audit finding B3 (2026-08-09): DRIVE_TIME_ESTIMATES bills EVERY intra-Disney hop at the one
flat cell ('Disney Resort','Disney Resort') = 12, whether it is the same resort or a
cross-property run. Port Orleans French Quarter -> Animal Kingdom Lodge is ~7 miles and ~24
real minutes, and that 12-minute underprice is precisely what let the founder's 2:03 PM
arrival chain into a 3:30 PM departure at exactly 0 spare.

The cost rule this file exists to protect (founder: "be mindful of API calls"):
chain_repo_minutes must NEVER make a network call. It reads route_distance's persistent,
process-cached table; the paid Distance Matrix call belongs to resolve_pending(), which runs
off the request path. If someone later wires the paid helper into the chain path, the
NoPaidCallsTests below fail.
"""
from unittest import mock

from django.core.cache import caches
from django.test import TestCase

import dispatching.scheduler as sch
from dispatching.analytics import categorize_location
from dispatching.scheduler import (
    chain_repo_minutes, chain_repo_needs_real_distance, check_feasibility,
)
from dispatching.route_distance import pair_hash
from dispatching.tests_founder_brain import _leg, _sched, D
from dispatching.tests_min_turn_buffer import AKL, MCO, POFQ, _cslot, FOUNDER_CLEAR_A

TAMPA = "412 N Willow Ave, Tampa, FL 33606, USA"


class PredicateTests(TestCase):
    def test_intra_cluster_pairs_qualify(self):
        self.assertTrue(chain_repo_needs_real_distance("Disney Resort", "Disney Resort"))
        self.assertTrue(chain_repo_needs_real_distance("Universal Resort", "Universal Resort"))

    def test_unplaceable_endpoints_qualify(self):
        self.assertTrue(chain_repo_needs_real_distance("Residential", "MCO Terminal"))
        self.assertTrue(chain_repo_needs_real_distance("MCO Terminal", "Other"))

    def test_well_known_cross_cluster_pairs_do_not(self):
        """MCO -> Disney is a route the table genuinely knows. Don't pay for it."""
        self.assertFalse(chain_repo_needs_real_distance("MCO Terminal", "Disney Resort"))
        self.assertFalse(chain_repo_needs_real_distance("Disney Resort", "MCO Terminal"))
        self.assertFalse(chain_repo_needs_real_distance("Disney Resort", "Universal Resort"))

    def test_predicate_matches_what_the_resolver_precomputes(self):
        """The chain path must only ask for pairs enqueue_upcoming_legs already fills —
        otherwise it widens the paid surface behind the founder's back."""
        for a, b in [("Disney Resort", "Disney Resort"), ("Residential", "MCO Terminal"),
                     ("MCO Terminal", "Disney Resort"), ("Other Hotel", "Disney Resort"),
                     ("Universal Resort", "Port Canaveral Area")]:
            enqueue_rule = (
                a in sch.LIVE_DISTANCE_UNKNOWN_CATS
                or b in sch.LIVE_DISTANCE_UNKNOWN_CATS
                or (a == b and a in sch.INTRA_CLUSTER_LIVE_CATS)
            )
            self.assertEqual(chain_repo_needs_real_distance(a, b), enqueue_rule, f"{a}->{b}")


class _ClearsRouteCache(TestCase):
    """route_distance keeps a 30-min PROCESS cache in front of the DB read, and Django does
    not flush locmem between tests — so a pair marked pending by one test would still read
    pending in the next. (Worth knowing in prod too: a pair stays stale in-process for up to
    30 minutes after the resolver fills it.)"""

    def setUp(self):
        super().setUp()
        caches["default"].clear()
        self.addCleanup(caches["default"].clear)


class CachedValueTests(_ClearsRouteCache):
    def test_precomputed_distance_wins_over_the_flat_cell(self):
        with mock.patch("dispatching.route_distance.cached_drive_minutes", return_value=24):
            self.assertEqual(
                chain_repo_minutes(POFQ, AKL, "Disney Resort", "Disney Resort"), 24)

    def test_cache_miss_falls_back_to_the_table(self):
        """Degrades to today's behaviour rather than blocking — an unresolved pair must not
        make the engine refuse work."""
        with mock.patch("dispatching.route_distance.cached_drive_minutes", return_value=None):
            self.assertEqual(
                chain_repo_minutes(POFQ, AKL, "Disney Resort", "Disney Resort"), 12)

    def test_cache_failure_falls_back_to_the_table(self):
        with mock.patch("dispatching.route_distance.cached_drive_minutes",
                        side_effect=RuntimeError("db down")):
            self.assertEqual(
                chain_repo_minutes(POFQ, AKL, "Disney Resort", "Disney Resort"), 12)

    def test_identical_address_short_circuits_before_any_lookup(self):
        with mock.patch("dispatching.route_distance.cached_drive_minutes") as cdm:
            self.assertEqual(chain_repo_minutes(POFQ, POFQ, "Disney Resort", "Disney Resort"), 0)
            cdm.assert_not_called()

    def test_known_route_never_consults_the_cache(self):
        with mock.patch("dispatching.route_distance.cached_drive_minutes") as cdm:
            self.assertEqual(chain_repo_minutes(MCO, POFQ, "MCO Terminal", "Disney Resort"), 30)
            cdm.assert_not_called()

    def test_far_address_now_reaches_the_cache_without_USE_LIVE_DISTANCE(self):
        """Before the fix this escape sat behind USE_LIVE_DISTANCE, which is default-OFF in
        prod — so chain math priced a Tampa run at the table's guess forever."""
        self.assertFalse(sch.USE_LIVE_DISTANCE, "prod default must stay OFF")
        with mock.patch("dispatching.route_distance.cached_drive_minutes", return_value=85):
            self.assertEqual(chain_repo_minutes(TAMPA, MCO, "Other", "MCO Terminal"), 85)

    def test_reads_a_real_row_end_to_end(self):
        from reservations.models import RouteDistanceCache
        RouteDistanceCache.objects.create(
            pair_hash=pair_hash(POFQ, AKL), pickup_text=POFQ, dropoff_text=AKL,
            status=RouteDistanceCache.STATUS_OK, drive_minutes=24)
        self.assertEqual(
            chain_repo_minutes(POFQ, AKL, "Disney Resort", "Disney Resort"), 24)

    def test_a_pending_row_is_not_treated_as_an_answer(self):
        from reservations.models import RouteDistanceCache
        RouteDistanceCache.objects.create(
            pair_hash=pair_hash(POFQ, AKL), pickup_text=POFQ, dropoff_text=AKL,
            status=RouteDistanceCache.STATUS_PENDING)
        self.assertEqual(
            chain_repo_minutes(POFQ, AKL, "Disney Resort", "Disney Resort"), 12)


class SameAddressTests(_ClearsRouteCache):
    """An airport terminal is one "address" covering a whole facility; a resort address is a
    single porte-cochère. The identical-address short-circuit must tell them apart."""

    def test_same_resort_address_is_zero(self):
        self.assertEqual(chain_repo_minutes(POFQ, POFQ, "Disney Resort", "Disney Resort"), 0)

    def test_same_airport_address_still_costs_the_self_pair(self):
        """Drop a departure at check-in, collect an arrival at baggage claim — real minutes.
        Returning 0 here silently desensitised the ops conflict scanner, which had been
        catching exactly this 2-minute margin (dispatching.tests_flight_change_safety)."""
        self.assertEqual(chain_repo_minutes("MCO", "MCO", "MCO Terminal", "MCO Terminal"), 2)
        self.assertEqual(chain_repo_minutes(MCO, MCO, "MCO Terminal", "MCO Terminal"), 2)

    def test_sanford_too(self):
        self.assertEqual(chain_repo_minutes("SFB", "SFB", "SFB Terminal", "SFB Terminal"), 2)

    def test_the_founder_same_terminal_turn_is_unaffected(self):
        """required_turnaround credits the deplaning grace for a same-terminal ARRIVAL and
        ignores the reposition entirely, so drop-at-1:35 / grab-the-1:34 still works."""
        ret = _leg(1, 13, 0, trip="return", pickup_loc=POFQ, dropoff_loc=MCO)
        board = _sched(1, [_cslot(ret, __import__("datetime").datetime(D.year, D.month, D.day, 13, 35))])
        arr = _leg(2, 13, 34, trip="arrival", pickup_loc=MCO, dropoff_loc=POFQ)
        self.assertTrue(check_feasibility(board, arr, D, min_buffer=10).feasible)


class NoPaidCallsTests(TestCase):
    """The founder's constraint, pinned. These fail loudly if the paid path creeps in."""

    def test_chain_repo_never_calls_distance_matrix(self):
        with mock.patch("drivers.utils.get_drive_time") as paid:
            for a, b, ca, cb in [
                (POFQ, AKL, "Disney Resort", "Disney Resort"),
                (TAMPA, MCO, "Other", "MCO Terminal"),
                (MCO, POFQ, "MCO Terminal", "Disney Resort"),
            ]:
                chain_repo_minutes(a, b, ca, cb)
            paid.assert_not_called()

    def test_a_whole_feasibility_check_never_calls_distance_matrix(self):
        board = _founder_board_local()
        leg_b = _leg(2, 15, 30, trip="return", pickup_loc=AKL, dropoff_loc=MCO)
        with mock.patch("drivers.utils.get_drive_time") as paid:
            check_feasibility(board, leg_b, D, min_buffer=5)
            paid.assert_not_called()

    def test_live_distance_flag_is_off_by_default(self):
        """USE_LIVE_DISTANCE=1 is the $593-spike path. It must stay opt-in."""
        self.assertFalse(sch.USE_LIVE_DISTANCE)


def _founder_board_local():
    leg_a = _leg(1, 14, 3, vtype="van", trip="arrival", pickup_loc=MCO, dropoff_loc=POFQ)
    return _sched(1, [_cslot(leg_a, FOUNDER_CLEAR_A)])


class FounderPairWithRealDistanceTests(_ClearsRouteCache):
    """The point of the whole fix: with the honest reposition the reported chain is
    infeasible on PHYSICS, before any buffer is applied."""

    def setUp(self):
        super().setUp()
        from reservations.models import RouteDistanceCache
        RouteDistanceCache.objects.create(
            pair_hash=pair_hash(POFQ, AKL), pickup_text=POFQ, dropoff_text=AKL,
            status=RouteDistanceCache.STATUS_OK, drive_minutes=24)

    def test_categories_still_bucket_the_same(self):
        self.assertEqual(categorize_location(POFQ), "Disney Resort")
        self.assertEqual(categorize_location(AKL), "Disney Resort")

    def test_rejected_even_with_no_buffer_at_all(self):
        leg_b = _leg(2, 15, 30, vtype="mini_van", trip="return",
                     pickup_loc=AKL, dropoff_loc=MCO)
        feas = check_feasibility(_founder_board_local(), leg_b, D, min_buffer=0)
        self.assertFalse(feas.feasible)
        self.assertEqual(feas.buffer_minutes, -12)
        self.assertIn("12 more min", feas.reason)
