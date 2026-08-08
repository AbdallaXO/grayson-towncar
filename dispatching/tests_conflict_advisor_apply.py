"""Recovery Advisor APPLY-path tests — the advisor rail's write endpoint core.

Run with:  ./manage.py test dispatching.tests_conflict_advisor_apply

What must hold (Stage C of the advisor plan):
  * Happy path: a reassign writes through set_leg_driver (THE front door) — attribution
    lands on driver_assigned_by, the reservations.signals AuditLog 'driver_assigned' row
    appears (so the actions module rightly adds no extra audit write), and SandboxLeakError
    never trips anywhere in this suite (the tripwire runs strict in tests).
  * STALENESS: expected-driver drift, expected_times drift, and a completed-mid-flight leg
    all => 409, nothing written.
  * FARM HARD GATES at the apply layer: VIP, true departures, and pending-refund legs are
    never farmed (the first two via the reused farmout_actions rules, the third the
    owner-confirmed advisor-only rule).
  * SNAPSHOT policy: trigger='conflict_advisor' for >=2-action or farm/retime plans; a
    single simple reassign skips (drag-drop parity).
  * Retime-only plans close their linked ops task explicitly ("Resolved via Recovery
    Advisor: <title>") — driver-change plans rely on the ops/signals auto-close.
  * HELD DAY (owner decision): applies go LIVE — without live_override_confirmed => 409
    with a clear message; with it, the write is live. stage=true + sandbox grant stages
    into the draft overlay instead (live board untouched).
  * Guard 6: picked-up legs never move (409); moving an on-the-way leg succeeds but the
    status resets to in-progress (Leg.save contract) and the response warns re-accept.
"""
from datetime import time, timedelta
from decimal import Decimal

from django.contrib.auth.models import Permission, User
from django.test import TestCase
from django.utils import timezone

from dispatching.conflict_advisor_actions import apply_advisor_plan
from dispatching.scheduler import preload_timing_cache
from drivers.models import (AffiliateProfile, Driver, DriverPayRate, DriverVehicleAssignment,
                            FleetVehicle)
from rates.models import Location, Rate, Route, Vehicle
from reservations.models import (AuditLog, Customer, DraftAssignment, Leg, RefundRequest,
                                 Reservation, ScheduleDraft, ScheduleSnapshot)

FUTURE = timezone.localdate() + timedelta(days=7)


class _AdvisorApplyFixture(TestCase):
    """Adapted from tests_farmout_apply._FarmoutApplyFixture: same board shapes,
    but exercised through apply_advisor_plan(data, user) directly (the advisor
    views land in a later stage)."""

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

        # Two in-house drivers with towncars on the test day (deployable +
        # vehicle-compatible receivers).
        cls.sam = Driver.objects.create(
            profile=User.objects.create_user("ra_sam", first_name="Sam"),
            driver_type="inhouse")
        cls.bob = Driver.objects.create(
            profile=User.objects.create_user("ra_bob", first_name="Bob"),
            driver_type="inhouse")
        for i, drv in enumerate((cls.sam, cls.bob)):
            fleet = FleetVehicle.objects.create(
                vehicle_number=f"T-{i + 1}", vehicle_type=cls.vehicle, year=2024,
                make="Lincoln", model="Continental")
            DriverVehicleAssignment.objects.create(driver=drv, date=FUTURE, vehicle=fleet)

        # Affiliate Waleed: single chain, $70 flat card.
        cls.waleed = Driver.objects.create(
            profile=User.objects.create_user("ra_waleed", first_name="Waleed"),
            driver_type="affiliate")
        DriverPayRate.objects.create(driver=cls.waleed, route=cls.route, vehicle=None,
                                     direction="both", base_pay=Decimal("70.00"))
        AffiliateProfile.objects.create(driver=cls.waleed, capacity_mode="single_chain",
                                        max_vehicle_tier="suv")

        cls.staff = User.objects.create_user("ra_staff", password="x", is_staff=True)

    def _leg(self, pickup_time=time(9, 0), driver=None, **kw):
        res_kw = kw.pop("reservation_kw", {})
        res = Reservation.objects.create(
            trip_type="one-way", customer=self.customer, rate=self.rate,
            vehicle=self.vehicle, base_price=Decimal("100.00"),
            total_price=Decimal("100.00"), **res_kw)
        defaults = dict(
            reservation=res, pickup_date=FUTURE, pickup_time=pickup_time,
            pickup_location="MCO", dropoff_location="Disney", route=self.route,
            status="confirmed", driver=driver)
        defaults.update(kw)
        return Leg.objects.create(**defaults)

    def _apply(self, payload, user=None):
        return apply_advisor_plan(payload, user or self.staff)

    @staticmethod
    def _payload(actions, expected, expected_times=None, **extra):
        p = {"schema": 1, "date": FUTURE.isoformat(),
             "disruption_id": "overlap:1:2", "plan_id": "overlap:1:2#p1",
             "task_id": None, "actions": actions,
             "expected": {str(k): v for k, v in expected.items()},
             "expected_times": {str(k): v for k, v in (expected_times or {}).items()}}
        p.update(extra)
        return p


class ReassignApplyTests(_AdvisorApplyFixture):
    def test_reassign_writes_through_front_door_with_attribution(self):
        leg = self._leg()
        status, body = self._apply(self._payload(
            [{"op": "reassign", "leg_id": leg.id, "to_driver_id": self.sam.id}],
            {leg.id: None}))
        self.assertEqual(status, 200, body)
        self.assertTrue(body["success"])
        self.assertEqual(body["mode"], "live")
        self.assertFalse(body["held"])
        leg.refresh_from_db()
        self.assertEqual(leg.driver_id, self.sam.id)
        self.assertEqual(leg.driver_assigned_by_id, self.staff.id)
        # set_leg_driver -> Leg.save() already raises the audit trail via
        # reservations.signals — the actions module adds NO extra AuditLog write.
        self.assertTrue(AuditLog.objects.filter(
            model_name="Leg", object_id=leg.id, action="driver_assigned").exists())
        # Snapshot policy: a single simple reassign skips (drag-drop parity).
        self.assertIsNone(body["snapshot_id"])
        self.assertEqual(ScheduleSnapshot.objects.count(), 0)

    def test_infeasible_after_drift_is_409_and_writes_nothing(self):
        self._leg(pickup_time=time(9, 0), driver=self.bob)  # Bob got busy after card time
        leg = self._leg(pickup_time=time(9, 0))
        status, body = self._apply(self._payload(
            [{"op": "reassign", "leg_id": leg.id, "to_driver_id": self.bob.id}],
            {leg.id: None}))
        self.assertEqual(status, 409)
        self.assertIn("new problem", body["error"])
        leg.refresh_from_db()
        self.assertIsNone(leg.driver_id)

    def test_malformed_payloads_are_400(self):
        leg = self._leg()
        base = self._payload(
            [{"op": "reassign", "leg_id": leg.id, "to_driver_id": self.sam.id}],
            {leg.id: None})
        for broken in (
            {**base, "schema": 2},
            {**base, "date": (timezone.localdate() - timedelta(days=1)).isoformat()},
            {**base, "actions": []},
            {**base, "actions": [{"op": "teleport", "leg_id": leg.id}]},
            {**base, "expected": None},
            {**base, "actions": [{"op": "retime", "leg_id": leg.id,
                                  "new_pickup_time": "25:99"}]},
        ):
            status, body = self._apply(broken)
            self.assertEqual(status, 400, body)
        leg.refresh_from_db()
        self.assertIsNone(leg.driver_id)


class StalenessTests(_AdvisorApplyFixture):
    def test_expected_driver_mismatch_is_409(self):
        leg = self._leg(driver=self.sam)  # someone assigned it after the card rendered
        status, body = self._apply(self._payload(
            [{"op": "reassign", "leg_id": leg.id, "to_driver_id": self.bob.id}],
            {leg.id: None}))
        self.assertEqual(status, 409)
        self.assertIn("board changed since this plan was computed", body["error"].lower())
        leg.refresh_from_db()
        self.assertEqual(leg.driver_id, self.sam.id)

    def test_expected_times_mismatch_is_409(self):
        leg = self._leg(pickup_time=time(9, 0), driver=self.sam)
        status, body = self._apply(self._payload(
            [{"op": "retime", "leg_id": leg.id, "new_pickup_time": "10:30"}],
            {leg.id: self.sam.id}, expected_times={leg.id: "09:30"}))
        self.assertEqual(status, 409)
        self.assertIn("board changed since this plan was computed", body["error"].lower())
        leg.refresh_from_db()
        self.assertEqual(leg.pickup_time, time(9, 0))

    def test_completed_mid_flight_is_409(self):
        leg = self._leg(driver=self.sam, status="completed")
        status, body = self._apply(self._payload(
            [{"op": "reassign", "leg_id": leg.id, "to_driver_id": self.bob.id}],
            {leg.id: self.sam.id}))
        self.assertEqual(status, 409)
        self.assertIn("completed", body["error"].lower())
        leg.refresh_from_db()
        self.assertEqual(leg.driver_id, self.sam.id)

    def test_missing_leg_is_404(self):
        status, _body = self._apply(self._payload(
            [{"op": "reassign", "leg_id": 999999, "to_driver_id": self.sam.id}],
            {999999: None}))
        self.assertEqual(status, 404)


class FarmGateTests(_AdvisorApplyFixture):
    def _farm(self, leg):
        return self._payload(
            [{"op": "farm_out", "leg_id": leg.id, "to_driver_id": self.waleed.id}],
            {leg.id: None})

    def test_vip_leg_is_never_farmed(self):
        leg = self._leg(reservation_kw={"is_vip": True})
        status, body = self._apply(self._farm(leg))
        self.assertEqual(status, 400)
        self.assertIn("vip", body["error"].lower())
        leg.refresh_from_db()
        self.assertIsNone(leg.driver_id)

    def test_departure_leg_is_never_farmed(self):
        leg = self._leg(pickup_location="Disney",
                        dropoff_location="Orlando International Airport (MCO)")
        status, body = self._apply(self._farm(leg))
        self.assertEqual(status, 400)
        self.assertIn("departure", body["error"].lower())
        leg.refresh_from_db()
        self.assertIsNone(leg.driver_id)

    def test_pending_refund_leg_is_never_farmed(self):
        leg = self._leg()
        RefundRequest.objects.create(
            reservation=leg.reservation, refund_type="full_cancellation",
            status="requested", reason="guest asked")
        status, body = self._apply(self._farm(leg))
        self.assertEqual(status, 400)
        self.assertIn("refund", body["error"].lower())
        leg.refresh_from_db()
        self.assertIsNone(leg.driver_id)

    def test_farm_happy_path_pays_from_card_and_says_call_to_confirm(self):
        from django.core.cache import cache
        from dispatching.conflict_advisor_actions import _farm_pending_key
        cache.delete(_farm_pending_key(FUTURE))   # isolate the reminder list
        leg = self._leg()
        status, body = self._apply(self._farm(leg))
        self.assertEqual(status, 200, body)
        leg.refresh_from_db()
        self.assertEqual(leg.driver_id, self.waleed.id)
        self.assertEqual(leg.driver_base_pay, Decimal("70.00"))
        # SOP: board assignment != acceptance — never marked resolved.
        self.assertIn("call", body["message"].lower())
        self.assertIn("Waleed", body["message"])
        self.assertIn("confirm", body["message"].lower())
        # The rail's "farmed — awaiting affiliate confirm" reminder is
        # recorded (the state endpoint turns it into a persistent card).
        from dispatching.conflict_advisor_actions import list_farm_pending
        pending = list_farm_pending(FUTURE)
        self.assertEqual([(e["leg_id"], e["affiliate"]) for e in pending],
                         [(leg.id, "Waleed")])

    def test_two_farm_actions_are_rejected(self):
        # The write path validates and pays exactly ONE affiliate
        # (farmout_actions._check_affiliate reads farm_writes[0] only) — a
        # second farm action would silently hand its leg to the FIRST
        # affiliate, ungated. The parser refuses the shape outright.
        leg_a = self._leg()
        leg_b = self._leg(pickup_time=time(15, 0))
        status, body = self._apply(self._payload(
            [{"op": "farm_out", "leg_id": leg_a.id, "to_driver_id": self.waleed.id},
             {"op": "farm_out", "leg_id": leg_b.id, "to_driver_id": self.waleed.id}],
            {leg_a.id: None, leg_b.id: None}))
        self.assertEqual(status, 400)
        self.assertIn("one farm", body["error"].lower())
        for leg in (leg_a, leg_b):
            leg.refresh_from_db()
            self.assertIsNone(leg.driver_id)


class TakebackApplyTests(_AdvisorApplyFixture):
    """Guard 7's write half: pulling an affiliate-committed leg back in-house
    requires the explicit confirm_pullback opt-in (reused farmout hard rule).
    The engine serializes the flag into every takeback plan's apply payload —
    without it the advisor's only affiliate recovery would be dead on arrival."""

    def test_takeback_without_confirm_pullback_is_400(self):
        leg = self._leg(driver=self.waleed)
        status, body = self._apply(self._payload(
            [{"op": "reassign", "leg_id": leg.id, "to_driver_id": self.sam.id}],
            {leg.id: self.waleed.id}))
        self.assertEqual(status, 400)
        self.assertIn("confirm_pullback", body["error"])
        leg.refresh_from_db()
        self.assertEqual(leg.driver_id, self.waleed.id)

    def test_takeback_with_engine_serialized_flag_succeeds(self):
        leg = self._leg(driver=self.waleed)
        status, body = self._apply(self._payload(
            [{"op": "reassign", "leg_id": leg.id, "to_driver_id": self.sam.id}],
            {leg.id: self.waleed.id}, confirm_pullback=True))
        self.assertEqual(status, 200, body)
        leg.refresh_from_db()
        self.assertEqual(leg.driver_id, self.sam.id)
        self.assertEqual(leg.driver_assigned_by_id, self.staff.id)


class SnapshotTests(_AdvisorApplyFixture):
    def test_two_move_plan_takes_a_conflict_advisor_snapshot(self):
        leg_a = self._leg(pickup_time=time(9, 0), driver=self.sam)
        leg_b = self._leg(pickup_time=time(15, 0))
        status, body = self._apply(self._payload(
            [{"op": "reassign", "leg_id": leg_a.id, "to_driver_id": self.bob.id},
             {"op": "reassign", "leg_id": leg_b.id, "to_driver_id": self.bob.id}],
            {leg_a.id: self.sam.id, leg_b.id: None}))
        self.assertEqual(status, 200, body)
        snap = ScheduleSnapshot.objects.get()
        self.assertEqual(snap.trigger, "conflict_advisor")
        self.assertEqual(body["snapshot_id"], snap.id)
        # Snapshot is PRE-write: leg_a's entry still shows Sam.
        entry = snap.entries.get(leg=leg_a)
        self.assertEqual(entry.driver_id, self.sam.id)
        leg_a.refresh_from_db(); leg_b.refresh_from_db()
        self.assertEqual(leg_a.driver_id, self.bob.id)
        self.assertEqual(leg_b.driver_id, self.bob.id)

    def test_single_reassign_skips_snapshot_but_retime_takes_one(self):
        leg = self._leg(pickup_time=time(9, 0), driver=self.sam)
        status, body = self._apply(self._payload(
            [{"op": "retime", "leg_id": leg.id, "new_pickup_time": "10:30"}],
            {leg.id: self.sam.id}, expected_times={leg.id: "09:00"}))
        self.assertEqual(status, 200, body)
        snap = ScheduleSnapshot.objects.get()
        self.assertEqual(snap.trigger, "conflict_advisor")
        self.assertEqual(body["snapshot_id"], snap.id)


class RetimeTaskTests(_AdvisorApplyFixture):
    def test_retime_only_plan_closes_its_task_with_advisor_attribution(self):
        from ops.models import OperationalTask
        from ops.services import create_task

        leg = self._leg(pickup_time=time(9, 0), driver=self.sam)
        task = create_task(OperationalTask.TaskType.TIGHT_TURN,
                           "Flight moved — match pickup", leg=leg)
        status, body = self._apply(self._payload(
            [{"op": "retime", "leg_id": leg.id, "new_pickup_time": "10:30",
              "note": "Flight match (Recovery Advisor)"}],
            {leg.id: self.sam.id}, expected_times={leg.id: "09:00"},
            task_id=task.id, title="Match the 9:00 AM pickup to its flight"))
        self.assertEqual(status, 200, body)
        leg.refresh_from_db()
        self.assertEqual(leg.pickup_time, time(10, 30))
        task.refresh_from_db()
        self.assertEqual(task.status, OperationalTask.Status.COMPLETED)
        self.assertEqual(task.resolved_by_id, self.staff.id)
        self.assertIn("Resolved via Recovery Advisor: Match the 9:00 AM pickup",
                      task.resolution_notes)
        self.assertEqual(body["closed_task_id"], task.id)
        # AuditLog row from apply_pickup_time_move (the Match-flight write path).
        self.assertTrue(AuditLog.objects.filter(
            model_name="Leg", object_id=leg.id, field_name="pickup_time").exists())

    def test_driver_change_plan_leaves_task_close_to_ops_signals(self):
        from ops.models import OperationalTask
        from ops.services import create_task

        leg = self._leg(pickup_time=time(9, 0), driver=self.sam)
        task = create_task(OperationalTask.TaskType.DRIVER_CONFLICT,
                           "Conflict on Sam", leg=leg,
                           metadata={"driver_id": self.sam.id})  # scanner shape
        status, body = self._apply(self._payload(
            [{"op": "reassign", "leg_id": leg.id, "to_driver_id": self.bob.id}],
            {leg.id: self.sam.id}, task_id=task.id))
        self.assertEqual(status, 200, body)
        self.assertIsNone(body["closed_task_id"])   # not closed by the actions module...
        task.refresh_from_db()
        # ...but the ops/signals auto-close fired on the driver change, attributed.
        self.assertEqual(task.status, OperationalTask.Status.COMPLETED)
        self.assertEqual(task.resolved_by_id, self.staff.id)


class HeldDayTests(_AdvisorApplyFixture):
    def _hold(self, user=None):
        return ScheduleDraft.objects.create(
            schedule_date=FUTURE, state=ScheduleDraft.State.DRAFT,
            created_by=user or self.staff)

    def test_held_day_without_confirm_flag_is_409_with_clear_message(self):
        self._hold()
        leg = self._leg()
        status, body = self._apply(self._payload(
            [{"op": "reassign", "leg_id": leg.id, "to_driver_id": self.sam.id}],
            {leg.id: None}))
        self.assertEqual(status, 409)
        self.assertIn("held", body["error"].lower())
        self.assertIn("live_override_confirmed", body["error"])
        leg.refresh_from_db()
        self.assertIsNone(leg.driver_id)

    def test_held_day_with_confirm_flag_writes_live(self):
        self._hold()
        leg = self._leg()
        status, body = self._apply(self._payload(
            [{"op": "reassign", "leg_id": leg.id, "to_driver_id": self.sam.id}],
            {leg.id: None}, live_override_confirmed=True))
        self.assertEqual(status, 200, body)
        self.assertEqual(body["mode"], "live")
        self.assertTrue(body["live_override"])
        leg.refresh_from_db()
        self.assertEqual(leg.driver_id, self.sam.id)   # LIVE write, owner policy

    def test_stage_true_with_sandbox_grant_stages_into_the_draft(self):
        granted = User.objects.create_user("ra_granted", password="x", is_staff=True)
        granted.user_permissions.add(
            Permission.objects.get(codename="use_schedule_sandbox"))
        granted = User.objects.get(pk=granted.pk)  # fresh perms cache
        draft = self._hold(granted)
        leg = self._leg()
        status, body = self._apply(self._payload(
            [{"op": "reassign", "leg_id": leg.id, "to_driver_id": self.sam.id}],
            {leg.id: None}, stage=True), user=granted)
        self.assertEqual(status, 200, body)
        self.assertTrue(body["held"])
        self.assertEqual(body["mode"], "staged")
        leg.refresh_from_db()
        self.assertIsNone(leg.driver_id)   # the sandbox no-leak invariant
        da = DraftAssignment.objects.get(draft=draft, leg=leg)
        self.assertEqual(da.proposed_driver_id, self.sam.id)

    def test_stage_true_without_grant_is_403(self):
        self._hold()
        leg = self._leg()
        status, body = self._apply(self._payload(
            [{"op": "reassign", "leg_id": leg.id, "to_driver_id": self.sam.id}],
            {leg.id: None}, stage=True))
        self.assertEqual(status, 403)
        leg.refresh_from_db()
        self.assertIsNone(leg.driver_id)


class StatusSafetyTests(_AdvisorApplyFixture):
    def test_picked_up_leg_never_moves(self):
        leg = self._leg(driver=self.sam, status="picked-up")
        status, body = self._apply(self._payload(
            [{"op": "reassign", "leg_id": leg.id, "to_driver_id": self.bob.id}],
            {leg.id: self.sam.id}))
        self.assertEqual(status, 409)
        self.assertIn("picked-up", body["error"])
        leg.refresh_from_db()
        self.assertEqual(leg.driver_id, self.sam.id)
        self.assertEqual(leg.status, "picked-up")

    def test_on_the_way_move_resets_status_and_warns_reaccept(self):
        leg = self._leg(driver=self.sam, status="on-the-way")
        status, body = self._apply(self._payload(
            [{"op": "reassign", "leg_id": leg.id, "to_driver_id": self.bob.id}],
            {leg.id: self.sam.id}))
        self.assertEqual(status, 200, body)
        leg.refresh_from_db()
        self.assertEqual(leg.driver_id, self.bob.id)
        # Leg.save() contract: progressed statuses belong to the previous
        # driver — the leg resets to in-progress for the new driver.
        self.assertEqual(leg.status, "in-progress")
        self.assertTrue(any("re-accept" in w for w in body["warnings"]))

    def test_a_pickup_that_already_happened_cannot_be_handed_over(self):
        """Guard 6b at the apply layer. The clock is re-derived at click time,
        not inherited from the card: a rail left open through a long phone call
        can still be showing a plan that was valid when it was drawn.

        Status is not a proxy for time — this leg is 'confirmed' because nobody
        ever tapped the app, and that is exactly the leg the swap search liked
        best (a long-gone slot has the widest free buffer)."""
        from dispatching.conflict_advisor_actions import _check_status_safety
        from dispatching.farmout_actions import PlanRejected

        leg = self._leg(driver=self.sam, pickup_time=time(16, 0),
                        pickup_date=FUTURE)
        plan = type("P", (), {"actions": [type("A", (), {
            "leg_id": leg.id, "op": "reassign"})()]})()
        legs = {leg.id: leg}

        # Same board, two clocks. Before the moment: allowed (the only thing
        # said about it is the pre-existing re-accept warning).
        leg.pickup_date = timezone.localdate() + timedelta(days=7)
        self.assertNotIn("come and gone",
                         " ".join(_check_status_safety(plan, legs)))

        # After it: 409, and the message names the pickup a dispatcher can find.
        leg.pickup_date = timezone.localdate() - timedelta(days=1)
        with self.assertRaises(PlanRejected) as cm:
            _check_status_safety(plan, legs)
        self.assertEqual(cm.exception.status, 409)
        self.assertIn("already come and gone", cm.exception.error)

    def test_an_unassigned_past_pickup_can_still_be_covered(self):
        """The carve-out, mirrored: nobody is at that curb yet, so covering a
        guest late is still exactly the right move."""
        from dispatching.conflict_advisor_actions import _check_status_safety

        leg = self._leg(driver=None, pickup_time=time(16, 0),
                        pickup_date=timezone.localdate() - timedelta(days=1))
        plan = type("P", (), {"actions": [type("A", (), {
            "leg_id": leg.id, "op": "reassign"})()]})()
        self.assertNotIn("come and gone",
                         " ".join(_check_status_safety(plan, {leg.id: leg})))


# ════════════════════════════════════════════════════════════════════════════
# STAGE G — pinned-scenario completions (plan Verification section)
# ════════════════════════════════════════════════════════════════════════════
from unittest.mock import patch


class DraftOverlayDriftTests(_AdvisorApplyFixture):
    def _granted(self):
        granted = User.objects.create_user("ra_ovl", password="x", is_staff=True)
        granted.user_permissions.add(
            Permission.objects.get(codename="use_schedule_sandbox"))
        return User.objects.get(pk=granted.pk)   # fresh perms cache

    def test_draft_overlay_drift_is_409_for_staged_apply(self):
        # 409-drift, draft-overlay variant: on a held day a STAGED apply
        # validates `expected` against the draft OVERLAY (the truth the
        # dispatcher is editing), not the live board. The overlay says Bob;
        # the plan was computed against an unassigned leg => 409 naming the
        # draft, nothing staged, live board untouched.
        granted = self._granted()
        draft = ScheduleDraft.objects.create(
            schedule_date=FUTURE, state=ScheduleDraft.State.DRAFT,
            created_by=granted)
        leg = self._leg()
        DraftAssignment.objects.create(draft=draft, leg=leg,
                                       proposed_driver=self.bob,
                                       assigned_by=granted)
        status, body = self._apply(self._payload(
            [{"op": "reassign", "leg_id": leg.id, "to_driver_id": self.sam.id}],
            {leg.id: None}, stage=True), user=granted)
        self.assertEqual(status, 409)
        self.assertIn("in the draft", body["error"])
        leg.refresh_from_db()
        self.assertIsNone(leg.driver_id)
        da = DraftAssignment.objects.get(draft=draft, leg=leg)
        self.assertEqual(da.proposed_driver_id, self.bob.id)  # overlay intact


class ApplyEnforceCapSplitTests(_AdvisorApplyFixture):
    def test_apply_revalidation_windows_resolved_with_enforce_cap_false(self):
        # The risk-review split, apply half: the dispatcher explicitly chose
        # this plan, so apply-time revalidation is manual-sovereign —
        # every window resolves with enforce_cap=False (matching
        # execute_swap/check_driver_feasibility; cap/window issues surface as
        # warnings, never auto-blocks). The generation half (enforce_cap=True)
        # is pinned in tests_conflict_advisor.
        from dispatching import feasibility_guards as fg
        leg = self._leg()
        seen = []
        real = fg.get_effective_window

        def spy(driver_id, configured=None, enforce_cap=True):
            seen.append(enforce_cap)
            return real(driver_id, configured=configured,
                        enforce_cap=enforce_cap)

        with patch.object(fg, "get_effective_window", side_effect=spy):
            status, body = self._apply(self._payload(
                [{"op": "reassign", "leg_id": leg.id,
                  "to_driver_id": self.sam.id}],
                {leg.id: None}))
        self.assertEqual(status, 200, body)
        self.assertTrue(seen, "apply revalidation never resolved a window")
        self.assertFalse(any(seen), "apply revalidation resolved a window "
                                    "with enforce_cap=True")


class TripwireArmedTests(_AdvisorApplyFixture):
    def test_sandbox_tripwire_is_armed_and_strict_in_this_suite(self):
        # The 'SandboxLeakError never trips' guarantee is only worth anything
        # if the tripwire is ARMED and STRICT while these tests run — pin
        # that: a raw Leg.driver write around the front door on a held day
        # must RAISE (not just log). Every green run of this suite is
        # therefore a real no-leak proof for the advisor's write path.
        from dispatching.assignment import SandboxLeakError
        ScheduleDraft.objects.create(
            schedule_date=FUTURE, state=ScheduleDraft.State.DRAFT,
            created_by=self.staff)
        leg = self._leg()
        leg.driver = self.sam
        with self.assertRaises(SandboxLeakError):
            leg.save()
        leg.refresh_from_db()
        self.assertIsNone(leg.driver_id)
