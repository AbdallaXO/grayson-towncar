"""Recovery Advisor endpoint tests — the rail's three thin views (Stage D).

Run with:  ./manage.py test dispatching.tests_recovery_advisor

What must hold:
  * AUTH matches farmout_apply: anonymous -> login redirect; authenticated
    non-staff AND staff-but-not-superuser -> JSON 403 on all three endpoints
    (the advisor is superuser-only during the owner trial); writes reject GET (405).
  * STATE: the fingerprint short-circuit answers {"changed": false} WITHOUT
    recomputing (compute_advisor_state never called); on change the full state
    comes back in the plan's response shape and is cached per (date, fp) so a
    second tab reuses the computation; ?leg= narrows via for_leg_id and bypasses
    that shared cache; the held flag mirrors the active ScheduleDraft; serving
    today's unfiltered board refreshes the ra_crit_count navbar mirror.
  * SNOOZE: filters the card out of subsequent state responses and reports the
    count; board-global (cache list, no per-user state); minutes capped at 240;
    expired snoozes stop filtering; malformed payloads -> 400.
  * APPLY: thin happy-path + error-passthrough only — deep validation coverage
    lives in tests_conflict_advisor_apply (the endpoint is a shim).
"""
from datetime import time, timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from dispatching.scheduler import preload_timing_cache
from drivers.models import Driver, DriverVehicleAssignment, FleetVehicle
from rates.models import Location, Rate, Route, Vehicle
from reservations.models import Customer, Leg, Reservation, ScheduleDraft

DAY = timezone.localdate() + timedelta(days=7)


def _card(card_id="overlap:1:2", severity="critical"):
    """A serialized disruption card in the engine's contract shape."""
    return {"id": card_id, "kind": "overlap", "severity": severity,
            "headline": "Sam's 9:00 and 9:05 overlap", "narrative": "",
            "impact_at": None, "leg_ids": [1, 2], "task_id": None,
            "basis": "clock_only", "plans": [], "detected_only": False,
            "no_internal_solution": False}


def _canned_state(cards, fp="fp-canned"):
    return {"fingerprint": fp, "computed_at": f"{DAY.isoformat()}T10:00",
            "truncated": False, "disruptions": cards}


class _RecoveryAdvisorFixture(TestCase):
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
            profile=User.objects.create_user("rv_sam", first_name="Sam"),
            driver_type="inhouse")
        fleet = FleetVehicle.objects.create(
            vehicle_number="T-1", vehicle_type=cls.vehicle, year=2024,
            make="Lincoln", model="Continental")
        DriverVehicleAssignment.objects.create(driver=cls.sam, date=DAY, vehicle=fleet)

        # SUPERUSER-ONLY during the owner trial (advisor_views.advisor_visible_to)
        # — a plain staff account is a dispatcher, and must be refused.
        cls.staff = User.objects.create_user(
            "rv_staff", password="x", is_staff=True, is_superuser=True)
        cls.nonstaff = User.objects.create_user("rv_plain", password="x")
        cls.staff_only = User.objects.create_user(
            "rv_dispatcher", password="x", is_staff=True)

    def setUp(self):
        cache.clear()   # ra_cards_* / ra_snoozed_* / ra_crit_count leak otherwise
        self.client.force_login(self.staff)

    def _leg(self, pickup_time=time(9, 0), driver=None, **kw):
        res = Reservation.objects.create(
            trip_type="one-way", customer=self.customer, rate=self.rate,
            vehicle=self.vehicle, base_price=Decimal("100.00"),
            total_price=Decimal("100.00"))
        defaults = dict(
            reservation=res, pickup_date=DAY, pickup_time=pickup_time,
            pickup_location="MCO", dropoff_location="Disney", route=self.route,
            status="confirmed", driver=driver)
        defaults.update(kw)
        return Leg.objects.create(**defaults)

    def _state(self, day=DAY, **params):
        params.setdefault("date", day.isoformat())
        return self.client.get(reverse("recovery_advisor_state"), params)

    def _snooze(self, disruption_id, day=DAY, **extra):
        payload = {"date": day.isoformat(), "disruption_id": disruption_id}
        payload.update(extra)
        return self.client.post(reverse("recovery_advisor_snooze"), payload,
                                content_type="application/json")


class AuthTests(_RecoveryAdvisorFixture):
    def test_anonymous_get_redirects_to_login(self):
        resp = Client().get(reverse("recovery_advisor_state"))
        self.assertEqual(resp.status_code, 302)
        self.assertIn("login", resp["Location"])

    def test_staff_dispatcher_is_403_while_superuser_only(self):
        """The trial gate: dispatchers ARE staff, so is_staff would have opened
        it to the whole floor. Flip advisor_views.advisor_visible_to to release."""
        self.client.force_login(self.staff_only)
        for resp in (self._state(),
                     self.client.post(reverse("recovery_advisor_apply"), {},
                                      content_type="application/json"),
                     self._snooze("overlap:1:2")):
            self.assertEqual(resp.status_code, 403)
            self.assertFalse(resp.json()["success"])

    def test_non_staff_is_403_json_on_all_three(self):
        self.client.force_login(self.nonstaff)
        checks = (
            self._state(),
            self.client.post(reverse("recovery_advisor_apply"), {},
                             content_type="application/json"),
            self._snooze("overlap:1:2"),
        )
        for resp in checks:
            self.assertEqual(resp.status_code, 403)
            self.assertFalse(resp.json()["success"])

    def test_write_endpoints_reject_get(self):
        self.assertEqual(
            self.client.get(reverse("recovery_advisor_apply")).status_code, 405)
        self.assertEqual(
            self.client.get(reverse("recovery_advisor_snooze")).status_code, 405)


class StateEndpointTests(_RecoveryAdvisorFixture):
    def test_empty_board_full_response_shape(self):
        # Real engine end-to-end over a quiet day: contract keys, no cards.
        resp = self._state()
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertTrue(body["changed"])
        # board sha1 + overlay digest (snoozes / farm-pending fold into the
        # short-circuit hash so their churn re-renders on a quiet board).
        self.assertRegex(body["fingerprint"], r"^[0-9a-f]{40}\.[0-9a-f]{10}$")
        self.assertIn("computed_at", body)
        self.assertFalse(body["truncated"])
        self.assertFalse(body["held"])
        self.assertFalse(body["can_stage"])
        self.assertEqual(body["snoozed"], 0)
        self.assertEqual(body["disruptions"], [])

    def test_fp_short_circuit_never_recomputes(self):
        fp = self._state().json()["fingerprint"]
        with patch("dispatching.conflict_advisor.compute_advisor_state") as m:
            body = self._state(fp=fp).json()
        self.assertEqual(body, {"changed": False, "fingerprint": fp})
        m.assert_not_called()

    def test_stale_fp_recomputes(self):
        fp = self._state(fp="not-the-current-fp").json()
        self.assertTrue(fp["changed"])

    def test_cards_cached_per_fingerprint_across_tabs(self):
        with patch("dispatching.conflict_advisor.compute_advisor_state",
                   return_value=_canned_state([_card()])) as m:
            first = self._state().json()
            second = self._state().json()
        self.assertEqual(m.call_count, 1)   # tab 2 rode the ra_cards_ cache
        self.assertEqual(first["disruptions"], second["disruptions"])
        self.assertEqual(len(first["disruptions"]), 1)

    def test_leg_filter_forwards_and_bypasses_shared_cache(self):
        with patch("dispatching.conflict_advisor.compute_advisor_state",
                   return_value=_canned_state([])) as m:
            self._state(leg="42")
            self._state(leg="42")
        self.assertEqual(m.call_count, 2)   # narrowed compute is never cached
        for call in m.call_args_list:
            self.assertEqual(call.kwargs.get("for_leg_id"), 42)

    def test_held_flag_reflects_active_draft(self):
        ScheduleDraft.objects.create(
            schedule_date=DAY, state=ScheduleDraft.State.DRAFT,
            created_by=self.staff)
        with patch("dispatching.conflict_advisor.compute_advisor_state",
                   return_value=_canned_state([])):
            self.assertTrue(self._state().json()["held"])

    def test_today_mirrors_visible_critical_count_for_navbar(self):
        today = timezone.localdate()
        cards = [_card("overlap:1:2", "critical"), _card("unassigned:9", "watch")]
        with patch("dispatching.conflict_advisor.compute_advisor_state",
                   return_value=_canned_state(cards)):
            self._state(day=today)
        self.assertEqual(cache.get("ra_crit_count"), 1)

    def test_future_day_never_touches_navbar_mirror(self):
        with patch("dispatching.conflict_advisor.compute_advisor_state",
                   return_value=_canned_state([_card()])):
            self._state()   # DAY is a week out
        self.assertIsNone(cache.get("ra_crit_count"))


class SnoozeTests(_RecoveryAdvisorFixture):
    def test_snooze_filters_card_and_reports_count(self):
        cards = [_card("overlap:1:2"), _card("unassigned:9", "watch")]
        resp = self._snooze("overlap:1:2")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertTrue(body["success"])
        self.assertEqual(body["snoozed_minutes"], 30)   # default
        self.assertEqual(body["active_snoozes"], 1)
        with patch("dispatching.conflict_advisor.compute_advisor_state",
                   return_value=_canned_state(cards)):
            state = self._state().json()
        self.assertEqual([c["id"] for c in state["disruptions"]], ["unassigned:9"])
        self.assertEqual(state["snoozed"], 1)

    def test_minutes_capped_at_240(self):
        body = self._snooze("overlap:1:2", minutes=999).json()
        self.assertEqual(body["snoozed_minutes"], 240)

    def test_expired_snooze_stops_filtering(self):
        import time as _time
        cache.set(f"ra_snoozed_{DAY.isoformat()}",
                  [{"id": "overlap:1:2", "until": _time.time() - 5}], 600)
        with patch("dispatching.conflict_advisor.compute_advisor_state",
                   return_value=_canned_state([_card("overlap:1:2")])):
            state = self._state().json()
        self.assertEqual(len(state["disruptions"]), 1)
        self.assertEqual(state["snoozed"], 0)

    def test_malformed_payloads_are_400(self):
        for resp in (
            self._snooze(""),                                  # no disruption_id
            self._snooze("overlap:1:2", date="not-a-date"),
            self._snooze("overlap:1:2", minutes="soonish"),
            self.client.post(reverse("recovery_advisor_snooze"), "{broken",
                             content_type="application/json"),
        ):
            self.assertEqual(resp.status_code, 400)
            self.assertFalse(resp.json()["success"])

    def test_snooze_changes_the_rail_fingerprint_for_every_tab(self):
        # The overlay digest folds active snoozes into the short-circuit hash:
        # dispatcher B (still holding the old fp) gets a FULL re-render on his
        # next poll instead of {"changed": false} hiding A's snooze; when the
        # snooze later expires the digest flips back and the card resurfaces
        # without waiting for a leg field to move.
        fp = self._state().json()["fingerprint"]
        self.assertFalse(self._state(fp=fp).json()["changed"])
        self._snooze("overlap:1:2")
        body = self._state(fp=fp).json()   # B's stale fp: full body now
        self.assertTrue(body["changed"])
        self.assertNotEqual(body["fingerprint"], fp)
        # Expiry flips the digest again (back to the no-overlay hash).
        cache.delete(f"ra_snoozed_{DAY.isoformat()}")
        body2 = self._state(fp=body["fingerprint"]).json()
        self.assertTrue(body2["changed"])
        self.assertEqual(body2["fingerprint"], fp)


class FarmPendingCardTests(_RecoveryAdvisorFixture):
    def test_farm_apply_reminder_rides_the_state_feed(self):
        # A farmed leg's card must not silently vanish: the apply path records
        # the farm and the rail shows "farmed — awaiting affiliate confirm"
        # until the reminder ages out (SOP: board assignment != acceptance).
        from dispatching.conflict_advisor_actions import record_farm_pending
        record_farm_pending(DAY, 4242, "Oualid")
        body = self._state().json()
        card = next(c for c in body["disruptions"]
                    if c["id"] == "farm_pending:4242")
        self.assertEqual(card["severity"], "watch")
        self.assertIn("awaiting Oualid confirm", card["headline"])
        self.assertIn("call Oualid to confirm", card["narrative"])
        self.assertEqual(card["plans"], [])
        # Snoozable like any card.
        self._snooze("farm_pending:4242")
        body = self._state().json()
        self.assertNotIn("farm_pending:4242",
                         [c["id"] for c in body["disruptions"]])


class ApplyEndpointTests(_RecoveryAdvisorFixture):
    """Thin shim coverage only — the deep validation matrix (staleness, hard
    rules, held-day, guard 6, snapshots) is pinned in tests_conflict_advisor_apply."""

    def _payload(self, actions, expected):
        return {"schema": 1, "date": DAY.isoformat(),
                "disruption_id": "overlap:1:2", "plan_id": "overlap:1:2#p1",
                "task_id": None, "actions": actions,
                "expected": {str(k): v for k, v in expected.items()},
                "expected_times": {}}

    def _apply(self, payload):
        return self.client.post(reverse("recovery_advisor_apply"), payload,
                                content_type="application/json")

    def test_happy_path_reassign_returns_contract(self):
        leg = self._leg()
        resp = self._apply(self._payload(
            [{"op": "reassign", "leg_id": leg.id, "to_driver_id": self.sam.id}],
            {leg.id: None}))
        self.assertEqual(resp.status_code, 200, resp.content)
        body = resp.json()
        self.assertTrue(body["success"])
        self.assertEqual(body["mode"], "live")
        for key in ("held", "live_override", "applied", "snapshot_id",
                    "closed_task_id", "warnings", "message"):
            self.assertIn(key, body)
        leg.refresh_from_db()
        self.assertEqual(leg.driver_id, self.sam.id)

    def test_error_status_passes_through(self):
        # Staleness drift -> apply_advisor_plan's 409 surfaces unchanged.
        leg = self._leg(driver=self.sam)
        resp = self._apply(self._payload(
            [{"op": "unassign", "leg_id": leg.id}], {leg.id: None}))
        self.assertEqual(resp.status_code, 409)
        self.assertFalse(resp.json()["success"])

    def test_invalid_json_is_400(self):
        resp = self.client.post(reverse("recovery_advisor_apply"), "{nope",
                                content_type="application/json")
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(resp.json()["success"])


class TemplateSmokeTests(_RecoveryAdvisorFixture):
    """Stage E surfacing: the rail is on the dashboard, the task page renders
    advisor plans (and collapses its no-feasibility-check candidates), and the
    navbar badge reads the cache mirror."""

    def _dashboard(self):
        return self.client.get(reverse("dashboard") + "?date=" + DAY.isoformat())

    def test_dashboard_renders_advisor_rail(self):
        resp = self._dashboard()
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'id="recoveryAdvisor"')
        # Config attrs the rail JS boots from, and the ra-* styles include.
        self.assertContains(resp, reverse("recovery_advisor_state"))
        self.assertContains(resp, reverse("recovery_advisor_apply"))
        self.assertContains(resp, reverse("recovery_advisor_snooze"))
        self.assertContains(resp, "ra-rail")

    def test_conflict_task_detail_renders_advisor_plans(self):
        from ops.models import OperationalTask
        leg = self._leg(driver=self.sam)
        task = OperationalTask.objects.create(
            task_type=OperationalTask.TaskType.DRIVER_CONFLICT, leg=leg,
            title="Driver conflict — Sam", priority=2,
            due_at=timezone.now() + timedelta(hours=1),
            metadata={"driver_id": self.sam.id, "pickup_date": DAY.isoformat()})
        page_ctx = {
            "driver_schedule": [{"leg": leg}], "driver_name": "Sam",
            "driver_phone": "", "conflict_minutes": 25,
            "pickup_date_str": DAY.isoformat(), "conflict_detail": None,
            "redesign": {"monitor_first": False},
            "ladder": {"inhouse": [], "affiliates": [],
                       "step3_unlocked": True, "checked": True},
            "can_match_flight": False,
        }
        card = _card(f"overlap:{leg.id}:99")
        card["leg_ids"] = [leg.id, 99]
        card["plans"] = [{
            "id": f"{card['id']}#p1", "rank": 1,
            "title": "Reassign the 9:00 MCO pickup to Marco",
            "why": ["Marco clears his prior job at 8:20 with 40 min to spare"],
            "risks": [], "farm_out": False, "price_impact": None,
            "moves": [{"leg_id": leg.id, "summary": "MCO → Disney · 9:00 AM",
                       "action": "reassign", "from": "Sam", "to": "Marco",
                       "resulting_slack_min": 40}],
            "apply": {"schema": 1, "date": DAY.isoformat(),
                      "disruption_id": card["id"],
                      "plan_id": f"{card['id']}#p1", "task_id": task.id,
                      "actions": [{"op": "reassign", "leg_id": leg.id,
                                   "to_driver_id": 7}],
                      "expected": {str(leg.id): self.sam.id},
                      "expected_times": {}},
        }]
        with patch("ops.views._build_driver_conflict_context",
                   return_value=page_ctx), \
             patch("ops.views._advisor_card_for_task", return_value=card):
            resp = self.client.get(reverse("task_detail", args=[task.id]))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'id="raTaskAdvisor"')
        self.assertContains(resp, "Reassign the 9:00 MCO pickup to Marco")
        self.assertContains(resp, "Apply this move")
        # Legacy no-feasibility-check candidates collapse behind a disclosure.
        self.assertContains(resp, "More options (no feasibility check)")
        self.assertContains(resp, 'id="raAdvisorCard"')   # json_script payload

    def test_conflict_task_detail_unchanged_without_card(self):
        from ops.models import OperationalTask
        leg = self._leg(driver=self.sam)
        task = OperationalTask.objects.create(
            task_type=OperationalTask.TaskType.DRIVER_CONFLICT, leg=leg,
            title="Driver conflict — Sam", priority=2,
            due_at=timezone.now() + timedelta(hours=1),
            metadata={"driver_id": self.sam.id, "pickup_date": DAY.isoformat()})
        page_ctx = {
            "driver_schedule": [{"leg": leg}], "driver_name": "Sam",
            "driver_phone": "", "conflict_minutes": 25,
            "pickup_date_str": DAY.isoformat(), "conflict_detail": None,
            "redesign": {"monitor_first": False},
            "ladder": {"inhouse": [], "affiliates": [],
                       "step3_unlocked": True, "checked": True},
            "can_match_flight": False,
        }
        with patch("ops.views._build_driver_conflict_context",
                   return_value=page_ctx), \
             patch("ops.views._advisor_card_for_task", return_value=None):
            resp = self.client.get(reverse("task_detail", args=[task.id]))
        self.assertEqual(resp.status_code, 200)
        self.assertNotContains(resp, 'id="raTaskAdvisor"')
        self.assertNotContains(resp, "More options (no feasibility check)")

    def test_navbar_badge_reads_cache_mirror_only(self):
        cache.set("ra_crit_count", 3, 60)
        resp = self._dashboard()
        self.assertContains(resp, 'id="raCritBadge"')
        self.assertContains(resp, ">3</span>")

    def test_navbar_badge_absent_when_cache_cold(self):
        resp = self._dashboard()   # setUp cleared the cache
        self.assertNotContains(resp, 'id="raCritBadge"')


# ════════════════════════════════════════════════════════════════════════════
# STAGE G — endpoint query budget (plan Verification section)
# ════════════════════════════════════════════════════════════════════════════
from django.db import connection as _connection
from django.test.utils import CaptureQueriesContext


class EndpointQueryBudgetTests(_RecoveryAdvisorFixture):
    def test_fp_short_circuit_endpoint_is_three_queries_plus_auth(self):
        # The 60 s poll's steady state: an unchanged-fingerprint GET must cost
        # exactly the 3-query fingerprint budget (legs+LegStatus cursor, DVA
        # roster, active draft) plus the test client's session+user auth reads
        # — nothing else, matching the driver-portal board_state poll it
        # copies.
        self._leg(driver=self.sam)
        fp = self._state().json()["fingerprint"]
        with CaptureQueriesContext(_connection) as ctx:
            resp = self._state(fp=fp)
        self.assertEqual(resp.json(), {"changed": False, "fingerprint": fp})
        real = [q for q in ctx.captured_queries
                if "SAVEPOINT" not in q["sql"].upper()]
        auth = [q for q in real if "django_session" in q["sql"]
                or "auth_user" in q["sql"]]
        self.assertLessEqual(len(real) - len(auth), 3,
                             "\n".join(q["sql"][:120] for q in real))
