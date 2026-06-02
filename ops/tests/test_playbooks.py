"""
Tests for the playbook-driven resolution ladder.

Two layers:
- Pure unit tests for ``ops.playbooks`` (config completeness + the stateful
  ``build_ladder_steps`` engine), using lightweight dict "attempts".
- An integration smoke test that the payment_chase task detail renders the
  redesigned page from real models with the ladder wired up.
"""

from datetime import datetime, timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from ops.models import OperationalTask
from ops.playbooks import (
    GENERIC_PLAYBOOK,
    PLAYBOOKS,
    build_ladder_steps,
    get_playbook,
    resolve_actions,
)


def _attempt(channel, outcome="sent"):
    """A minimal stand-in for a CommunicationAttempt row."""
    return {"channel": channel, "outcome": outcome}


def _by_id(steps):
    return {s["id"]: s for s in steps}


# ── Config completeness ──────────────────────────────────────────────────────


class PlaybookConfigTests(TestCase):
    def test_every_task_type_has_a_playbook(self):
        for value, _label in OperationalTask.TaskType.choices:
            self.assertIn(value, PLAYBOOKS, f"missing playbook for {value}")

    def test_unknown_type_falls_back_to_generic(self):
        self.assertIs(get_playbook("does_not_exist"), GENERIC_PLAYBOOK)
        self.assertEqual(get_playbook("does_not_exist")["steps"], [])

    def test_payment_chase_is_fully_defined(self):
        pb = get_playbook(OperationalTask.TaskType.PAYMENT_CHASE)
        ids = [s["id"] for s in pb["steps"]]
        self.assertEqual(ids, ["call", "text", "email"])
        call = pb["steps"][0]
        self.assertEqual(call["channel"], "call")
        self.assertTrue(call["recommended"])
        self.assertIn("answered", call["branches"])
        self.assertIn("no_answer", call["branches"])
        self.assertEqual(pb["resolves_when"], "paid_or_cancelled")

    def test_stub_playbooks_render_steps_with_icons(self):
        # Every non-manual stub must produce renderable steps (id/label/icon).
        for value, _ in OperationalTask.TaskType.choices:
            if value == "manual":
                continue
            steps = build_ladder_steps(get_playbook(value), [])
            self.assertTrue(steps, f"{value} produced no steps")
            for s in steps:
                self.assertTrue(s["label"])
                self.assertTrue(s["icon"].startswith("bi-"))


# ── Stateful ladder engine ───────────────────────────────────────────────────


class LadderStateTests(TestCase):
    def setUp(self):
        self.pb = get_playbook(OperationalTask.TaskType.PAYMENT_CHASE)

    def test_no_attempts_recommends_first_step(self):
        steps = _by_id(build_ladder_steps(self.pb, []))
        self.assertEqual(steps["call"]["state"], "recommended")
        self.assertTrue(steps["call"]["recommended"])
        self.assertEqual(steps["text"]["state"], "available")
        self.assertEqual(steps["email"]["state"], "available")
        # No pills when nothing has been tried.
        self.assertEqual(steps["email"]["tried_pill"], "")

    def test_tried_channel_is_deemphasized_with_pill(self):
        steps = _by_id(build_ladder_steps(self.pb, [_attempt("email"), _attempt("email")]))
        self.assertEqual(steps["email"]["state"], "deemphasized")
        self.assertEqual(steps["email"]["tried_pill"], "sent ×2")
        # Call is still untried → it remains the recommended step.
        self.assertEqual(steps["call"]["state"], "recommended")

    def test_call_pill_uses_tried_verb(self):
        steps = _by_id(build_ladder_steps(self.pb, [_attempt("call", "no_answer")]))
        self.assertEqual(steps["call"]["tried_pill"], "tried ×1")

    def test_no_answer_advances_highlight_to_next_step(self):
        steps = _by_id(build_ladder_steps(self.pb, [_attempt("call", "no_answer")]))
        # Call was tried but not connected → highlight moves to Text.
        self.assertEqual(steps["call"]["state"], "deemphasized")
        self.assertEqual(steps["text"]["state"], "recommended")
        self.assertTrue(steps["text"]["recommended"])

    def test_answered_marks_call_satisfied(self):
        steps = _by_id(build_ladder_steps(self.pb, [_attempt("call", "answered")]))
        # A connected call is no longer the recommended action.
        self.assertFalse(steps["call"]["recommended"])
        self.assertEqual(steps["text"]["state"], "recommended")

    def test_soft_nudge_points_back_to_recommended(self):
        steps = _by_id(build_ladder_steps(self.pb, []))
        self.assertEqual(steps["call"]["nudge"], "")  # the recommended step has no nudge
        self.assertIn("call", steps["text"]["nudge"].lower())
        self.assertIn("call", steps["email"]["nudge"].lower())

    def test_all_tried_falls_back_to_config_recommended(self):
        steps = _by_id(build_ladder_steps(
            self.pb,
            [_attempt("call", "no_answer"), _attempt("sms"), _attempt("email")],
        ))
        # Nothing connected, every step tried → fall back to the config
        # recommended step (the call) rather than leaving nothing highlighted.
        self.assertTrue(steps["call"]["recommended"])
        self.assertEqual(steps["call"]["state"], "recommended")

    def test_manual_playbook_has_no_ladder(self):
        self.assertEqual(build_ladder_steps(get_playbook("manual"), []), [])

    def test_coverage_cascade_honors_config_recommended_when_fresh(self):
        # driver_conflict steps are all non-comm; a fresh task should highlight
        # the flagged "cover in-house" step, NOT just the first step.
        steps = _by_id(build_ladder_steps(get_playbook("driver_conflict"), []))
        self.assertTrue(steps["cover_in_house"]["recommended"])
        self.assertFalse(steps["match_flight"]["recommended"])
        # Non-comm steps are never de-emphasized.
        for s in steps.values():
            self.assertNotEqual(s["state"], "deemphasized")
        # Steps after the recommended one still get a soft nudge.
        self.assertTrue(steps["farm_out"]["nudge"])

    def test_accepts_objects_not_only_dicts(self):
        class _A:
            def __init__(self, channel, outcome):
                self.channel = channel
                self.outcome = outcome

        steps = _by_id(build_ladder_steps(self.pb, [_A("email", "sent")]))
        self.assertEqual(steps["email"]["tried_pill"], "sent ×1")


class ResolveActionsTests(TestCase):
    def test_resolves_known_action_ids(self):
        actions = resolve_actions(["take_payment", "cancel_trip"])
        self.assertEqual([a["id"] for a in actions], ["take_payment", "cancel_trip"])
        self.assertEqual(actions[0]["kind"], "payment")
        self.assertEqual(actions[1]["kind"], "cancel")

    def test_skips_unknown_and_handles_none(self):
        self.assertEqual(resolve_actions(["bogus"]), [])
        self.assertEqual(resolve_actions(None), [])


# ── Integration: the redesigned page renders from real data ──────────────────


class PaymentChasePageTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        from rates.models import Location, Rate, Route, Vehicle
        from reservations.models import Customer, Leg, Reservation

        cls.User = get_user_model()
        cls.staff = cls.User.objects.create_user(
            username="dispatcher", password="x", is_staff=True, is_superuser=True
        )
        cls.staff.first_name = "Dee"
        cls.staff.save(update_fields=["first_name"])

        vehicle = Vehicle.objects.create(
            vehicle_type="towncar", capacity=4, luggage_capacity=4
        )
        origin = Location.objects.create(name="MCO Airport")
        dest = Location.objects.create(name="Disney Resort")
        route = Route.objects.create(origin=origin, destination=dest)
        rate = Rate.objects.create(
            vehicle=vehicle, route=route,
            oneway_price=Decimal("295.00"), round_trip_price=Decimal("520.00"),
        )
        cls.customer = Customer.objects.create(
            first_name="Rakesh", last_name="Patel", email="rakesh@example.com",
            phone_number="(407) 555-0142", zipcode="32801",
        )
        cls.res = Reservation.objects.create(
            customer=cls.customer, rate=rate, vehicle=vehicle, trip_type="one_way",
            base_price=Decimal("295.00"), total_price=Decimal("295.00"),
            status="confirmed",
        )
        pickup = timezone.localdate() + timedelta(days=3)
        Leg.objects.create(
            reservation=cls.res,
            pickup_date=pickup, pickup_time=datetime(2026, 6, 5, 15, 40).time(),
            pickup_location="MCO Airport", dropoff_location="Disney Resort",
            status="confirmed",
        )
        cls.task = OperationalTask.objects.create(
            task_type=OperationalTask.TaskType.PAYMENT_CHASE,
            status=OperationalTask.Status.PENDING,
            priority=OperationalTask.Priority.HIGH,
            title="Unpaid balance — Rakesh Patel",
            reservation=cls.res,
            due_at=timezone.now() + timedelta(hours=2),
            escalate_at=timezone.now() + timedelta(hours=6),
        )

    def setUp(self):
        self.client.force_login(self.staff)

    def test_renders_redesigned_payment_page(self):
        resp = self.client.get(reverse("task_detail", args=[self.task.id]))
        self.assertEqual(resp.status_code, 200)
        self.assertTemplateUsed(resp, "dispatching/payment_task_detail.html")
        self.assertTemplateUsed(resp, "dispatching/includes/_resolution_ladder.html")

    def test_context_has_ladder_and_wiring(self):
        resp = self.client.get(reverse("task_detail", args=[self.task.id]))
        ctx = resp.context
        self.assertTrue(ctx["payment_redesign"])
        self.assertEqual(len(ctx["ladder_steps"]), 3)
        # Call step carries its resolve actions (take payment / cancel trip).
        call_step = next(s for s in ctx["ladder_steps"] if s["id"] == "call")
        self.assertEqual(
            [a["id"] for a in call_step["answered_actions"]],
            ["take_payment", "cancel_trip"],
        )
        lc = ctx["ladder_ctx"]
        self.assertIn(str(self.res.uuid), lc["portal_url"])
        self.assertIn("/payment/checkout-session/", lc["sms_draft"])
        self.assertEqual(lc["phone_href"], "4075550142")
        self.assertEqual(ctx["pc_amount_owed"], Decimal("295.00"))

    def test_page_shows_guest_and_owed(self):
        resp = self.client.get(reverse("task_detail", args=[self.task.id]))
        html = resp.content.decode()
        self.assertIn("Unpaid Balance — Rakesh Patel", html)
        self.assertIn("Collection Ladder", html)
        self.assertIn("295.00", html)
