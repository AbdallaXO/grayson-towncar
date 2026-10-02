"""Dispatch lead and refunds — the Dispatch Lead group approves refunds.

Founder decision 2026-10-02: a team lead approves or rejects any refund request,
their own included, from the same Refunds page as an admin — single, bulk, and
the type fix before the money goes. Correcting a refund that already went
through stays admin-only, and so does filing a second request while one waits.

Also pinned: a request that was already decided is never re-typed or
re-decided — with two people working one queue, the other may get there first.

Run with:
  ENABLE_DEBUG_TOOLBAR=0 python manage.py test dispatching.tests_dispatch_lead_refunds
"""
import json
from decimal import Decimal
from unittest import mock

from django.contrib.auth.models import Group, User
from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse

from dispatching.tests_refund_correction import _RefundFixtureMixin
from reservations.models import RefundRequest

# Same reason as tests_refund_correction: signal side effects spawn background
# threads that race the test's own SQLite writes.
_NOOP = lambda *a, **k: None
_bg_targets = [
    "reservations.utils._run_in_background",
    "drivers.signals._run_in_background",
    "dispatching.views._run_in_background",
]
_bg_patchers = []


def setUpModule():
    for target in _bg_targets:
        try:
            p = mock.patch(target, _NOOP)
            p.start()
            _bg_patchers.append(p)
        except (AttributeError, ModuleNotFoundError):
            pass


def tearDownModule():
    for p in _bg_patchers:
        p.stop()
    _bg_patchers.clear()


def _lead(username="rf_lead"):
    user = User.objects.create_user(username, password="x", is_staff=True, first_name="Lena")
    user.groups.add(Group.objects.get(name="Dispatch Lead"))
    return User.objects.get(pk=user.pk)  # fresh permission cache


class _LeadRefundMixin(_RefundFixtureMixin):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.lead = _lead()

    def setUp(self):
        cache.clear()  # the Refunds pill is cached for the whole floor
        self.client.force_login(self.lead)

    def _process(self, rr, action="approve", refund_type=None):
        payload = {"refund_request_id": rr.id, "action": action}
        if refund_type:
            payload["refund_type"] = refund_type
        return self.client.post(
            reverse("process_refund"), json.dumps(payload), content_type="application/json",
        )


@mock.patch("dispatching.views.stripe.Refund.create", return_value=mock.Mock(id="re_lead"))
class LeadApprovesRefundsTests(_LeadRefundMixin, TestCase):
    def test_lead_approves_a_teammates_refund(self, mock_refund):
        _, legs, rr = self._make_requested("price_adjustment", "40.00")
        r = self._process(rr)
        self.assertEqual(r.status_code, 200, r.content)
        rr.refresh_from_db()
        self.assertEqual(rr.status, "completed")
        self.assertEqual(rr.processed_by, self.lead)
        self.assertTrue(mock_refund.called)
        legs[0].refresh_from_db()
        self.assertEqual(legs[0].status, "confirmed")  # a price adjustment cancels nothing

    def test_lead_approves_their_own_refund(self, mock_refund):
        _, _, rr = self._make_requested("price_adjustment", "25.00")
        rr.requested_by = self.lead
        rr.save(update_fields=["requested_by"])
        r = self._process(rr)
        self.assertEqual(r.status_code, 200, r.content)
        rr.refresh_from_db()
        self.assertEqual(rr.status, "completed")
        self.assertEqual(rr.processed_by, self.lead)

    def test_lead_fixes_the_type_before_approving(self, mock_refund):
        res, legs, rr = self._make_requested("full_cancellation", "40.00")
        r = self._process(rr, refund_type="price_adjustment")
        self.assertEqual(r.status_code, 200, r.content)
        rr.refresh_from_db()
        self.assertEqual(rr.refund_type, "price_adjustment")
        res.refresh_from_db()
        self.assertNotEqual(res.status, "cancelled")  # the booking still runs

    def test_lead_rejects(self, mock_refund):
        _, _, rr = self._make_requested("price_adjustment", "40.00")
        r = self._process(rr, action="reject")
        self.assertEqual(r.status_code, 200, r.content)
        rr.refresh_from_db()
        self.assertEqual(rr.status, "rejected")
        self.assertEqual(rr.processed_by, self.lead)
        self.assertFalse(mock_refund.called)

    def test_lead_bulk_approves(self, mock_refund):
        _, _, rr1 = self._make_requested("price_adjustment", "40.00")
        _, _, rr2 = self._make_requested("partial_cancellation", "100.00")
        r = self.client.post(
            reverse("bulk_approve_refunds"),
            json.dumps({"refund_request_ids": [rr1.id, rr2.id]}),
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.json()["approved_count"], 2)

    def test_lead_cannot_correct_a_processed_refund(self, mock_refund):
        res, legs, _, rr = self._make_full_cancelled()
        r = self.client.post(
            reverse("correct_refund"),
            json.dumps({"refund_request_id": rr.id, "new_refund_type": "price_adjustment"}),
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 403)
        rr.refresh_from_db()
        self.assertEqual(rr.refund_type, "full_cancellation")
        res.refresh_from_db()
        self.assertEqual(res.status, "cancelled")  # legs not restored

    def test_lead_cannot_file_a_second_request_while_one_waits(self, mock_refund):
        res, _, _ = self._make_requested("price_adjustment", "40.00")
        r = self.client.post(
            reverse("request_refund"),
            json.dumps({
                "reservation_uuid": str(res.uuid),
                "refund_reason": "another one",
                "refund_amount": "10.00",
                "refund_type": "price_adjustment",
            }),
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 400)
        self.assertEqual(RefundRequest.objects.filter(reservation=res).count(), 1)


@mock.patch("dispatching.views.stripe.Refund.create", return_value=mock.Mock(id="re_lead"))
class DecidedRefundIsFinalTests(_LeadRefundMixin, TestCase):
    """Two people work the queue now: a stale page must not rewrite history."""

    def test_completed_refund_is_not_retyped_by_a_stale_approve(self, mock_refund):
        res, legs, _, rr = self._make_full_cancelled()
        r = self._process(rr, refund_type="price_adjustment")
        self.assertEqual(r.status_code, 409)
        self.assertIn("already completed", r.json()["error"])
        rr.refresh_from_db()
        self.assertEqual(rr.refund_type, "full_cancellation")
        self.assertEqual(rr.status, "completed")
        self.assertFalse(mock_refund.called)

    def test_completed_refund_cannot_be_rejected(self, mock_refund):
        _, _, _, rr = self._make_full_cancelled()
        r = self._process(rr, action="reject")
        self.assertEqual(r.status_code, 409)
        rr.refresh_from_db()
        self.assertEqual(rr.status, "completed")

    def test_rejected_refund_cannot_then_be_approved(self, mock_refund):
        _, _, rr = self._make_requested("price_adjustment", "40.00")
        self._process(rr, action="reject")
        r = self._process(rr)
        self.assertEqual(r.status_code, 409)
        rr.refresh_from_db()
        self.assertEqual(rr.status, "rejected")
        self.assertFalse(mock_refund.called)

    def test_an_admin_is_held_to_the_same_rule(self, mock_refund):
        self.client.force_login(self.manager)
        _, _, _, rr = self._make_full_cancelled()
        r = self._process(rr, refund_type="price_adjustment")
        self.assertEqual(r.status_code, 409)
        rr.refresh_from_db()
        self.assertEqual(rr.refund_type, "full_cancellation")

    def test_a_rejected_refund_is_not_rejected_again(self, mock_refund):
        _, _, rr = self._make_requested("price_adjustment", "40.00")
        self.client.force_login(self.manager)
        self._process(rr, action="reject")
        self.client.force_login(self.lead)
        r = self._process(rr, action="reject")
        self.assertEqual(r.status_code, 409)
        self.assertIn("already rejected", r.json()["error"])
        rr.refresh_from_db()
        self.assertEqual(rr.processed_by, self.manager)  # the first decision stands

    def test_a_rejected_refund_is_not_retyped(self, mock_refund):
        _, _, rr = self._make_requested("price_adjustment", "40.00")
        self._process(rr, action="reject")
        r = self._process(rr, refund_type="full_cancellation")
        self.assertEqual(r.status_code, 409)
        rr.refresh_from_db()
        self.assertEqual(rr.refund_type, "price_adjustment")
        self.assertFalse(mock_refund.called)


@mock.patch("dispatching.views.stripe.Refund.create", return_value=mock.Mock(id="re_lead"))
class RefundGoingThroughTests(_LeadRefundMixin, TestCase):
    """While one approver's Stripe call is in flight the request is 'processing'.
    Nobody else may decide it then: a reject would be overwritten by the finish,
    and a second claim could refund a second payment."""

    def _in_flight(self, refund_type="price_adjustment", amount="40.00"):
        _, legs, rr = self._make_requested(refund_type, amount)
        RefundRequest.objects.filter(pk=rr.pk).update(status="processing")
        return legs, rr

    def test_it_cannot_be_rejected(self, mock_refund):
        _, rr = self._in_flight()
        r = self._process(rr, action="reject")
        self.assertEqual(r.status_code, 409)
        self.assertIn("going through right now", r.json()["error"])
        rr.refresh_from_db()
        self.assertEqual(rr.status, "processing")
        self.assertIsNone(rr.processed_by)

    def test_it_cannot_be_approved_or_retyped_a_second_time(self, mock_refund):
        legs, rr = self._in_flight()
        r = self._process(rr, refund_type="full_cancellation")
        self.assertEqual(r.status_code, 409)
        rr.refresh_from_db()
        self.assertEqual(rr.refund_type, "price_adjustment")
        self.assertFalse(mock_refund.called)
        legs[0].refresh_from_db()
        self.assertEqual(legs[0].status, "confirmed")

    def test_the_claim_holds_even_past_a_stale_check(self, mock_refund):
        # The view's early check read 'requested'; by the time the claim runs
        # somebody else has it. The conditional UPDATE is what decides.
        from dispatching.views import _execute_refund_approval

        _, rr = self._in_flight()
        rr.status = "requested"  # what the stale reader saw
        result = _execute_refund_approval(rr, self.lead, refund_type="full_cancellation")
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], 409)
        rr.refresh_from_db()
        self.assertEqual(rr.refund_type, "price_adjustment")
        self.assertFalse(mock_refund.called)

    def test_a_total_stripe_failure_puts_it_back_in_the_queue(self, mock_refund):
        import stripe

        mock_refund.side_effect = stripe.error.StripeError("Stripe is down")
        _, _, rr = self._make_requested("price_adjustment", "40.00")
        r = self._process(rr)
        self.assertEqual(r.status_code, 500)
        rr.refresh_from_db()
        self.assertEqual(rr.status, "requested")  # retryable, not stuck


class LeadRefundsPageTests(_LeadRefundMixin, TestCase):
    def test_lead_opens_the_refunds_page_without_the_correct_button(self):
        self._make_full_cancelled()
        r = self.client.get(reverse("refund_management") + "?review=full_cancellations")
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.context["can_correct"])
        self.assertNotContains(r, "bi-wrench-adjustable")
        self.assertContains(r, "tell Abdalla")

    def test_admin_still_gets_the_correct_button(self):
        self.client.force_login(self.manager)
        self._make_full_cancelled()
        r = self.client.get(reverse("refund_management") + "?review=full_cancellations")
        self.assertTrue(r.context["can_correct"])
        self.assertContains(r, "bi-wrench-adjustable")

    def test_plain_dispatcher_is_turned_away(self):
        self.client.force_login(self.dispatcher)
        r = self.client.get(reverse("refund_management"))
        self.assertRedirects(r, reverse("dashboard"), fetch_redirect_response=False)

    def test_plain_dispatcher_cannot_approve(self):
        _, _, rr = self._make_requested("price_adjustment", "40.00")
        self.client.force_login(self.dispatcher)
        r = self._process(rr)
        self.assertEqual(r.status_code, 403)
        rr.refresh_from_db()
        self.assertEqual(rr.status, "requested")

    def test_refunds_link_shows_for_the_lead_only(self):
        link = f'href="{reverse("refund_management")}"'
        r = self.client.get(reverse("timeclock"))
        self.assertContains(r, link)
        self.client.force_login(self.dispatcher)
        r = self.client.get(reverse("timeclock"))
        self.assertNotContains(r, link)

    @mock.patch("dispatching.views.stripe.Refund.create", return_value=mock.Mock(id="re_lead"))
    def test_waiting_count_drops_the_moment_one_is_decided(self, mock_refund):
        _, _, rr1 = self._make_requested("price_adjustment", "40.00")
        self._make_requested("price_adjustment", "15.00")
        r = self.client.get(reverse("timeclock"))
        self.assertEqual(r.context["pending_refund_count"], 2)

        self._process(rr1)
        r = self.client.get(reverse("timeclock"))
        self.assertEqual(r.context["pending_refund_count"], 1)

    def test_plain_dispatcher_gets_no_waiting_count(self):
        self._make_requested("price_adjustment", "40.00")
        self.client.force_login(self.dispatcher)
        r = self.client.get(reverse("timeclock"))
        self.assertNotIn("pending_refund_count", r.context)
