"""
Date-anchored pre-pickup nudge engine.

Fires ONE SMS ~3 days before a lead's pickup date to warm-but-unbooked leads —
crucially including those whose 5-step form sequence already ended or was
cancelled by a reply (a lead who said "let me think" weeks ago and never booked
is the core target). It is a SEPARATE scanner from the cancellable form sequence
(``ghl_integration/tasks.py``) but REUSES that system's plumbing: the GHL send
path, the send-window helpers (``ghl_integration/timing.py``), the editable
``FollowUpSequence`` templates, the message renderer, and the SMS→email fallback.
One nudge per lead, ever (idempotent — safe to run every cycle).

Modeled on ``ops/unpaid_reminders.py`` (engine class, module toggles, dry-run,
result dataclass). Invoked hourly from the scheduler batch and via the
``send_pre_pickup_nudges`` management command.

Offer variants (first match wins):
  discount        → routed to a HUMAN (NO automated SMS). The booking flow has no
                    coupon/price-override (only customer-entered Stripe promo
                    codes), so we never auto-quote a price the self-serve link
                    won't honor. Owner decision.
  cruise_urgency  → cruise-transfer leads (segment == "cruise_transfer").
  urgency         → default; always safe.
(The free-upgrade variant is intentionally out of scope for v1.)
"""

from __future__ import annotations

import logging
import time as time_module
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, time as dt_time, timedelta
from decimal import Decimal

from django.db import IntegrityError
from django.utils import timezone

logger = logging.getLogger(__name__)


# ── Constants & toggles ───────────────────────────────────────────────────────

NUDGE_STEP = 6

# Eligible when (pickup_date - today) in calendar days is within this inclusive
# range. pickup_date is DATE-ONLY; the 2-day width tolerates a missed hourly
# cycle while the (lead, NUDGE_STEP) unique row guarantees exactly one send.
PICKUP_DAYS_MIN = 2
PICKUP_DAYS_MAX = 3

# Adjacency throttle: skip if ANY outbound to the lead within this many hours, so
# a nudge can't land on top of a recent message in the final days. Anchored on
# locally-visible signals (see _last_outbound_at) — a rep texting from GHL
# directly may not be visible; that residual risk is flagged in the plan.
MIN_OUTBOUND_GAP_HOURS = 48

# Forward-collision guard: skip if a PENDING form-sequence step is due within
# this many hours, so the nudge doesn't immediately precede a queued follow-up.
FORWARD_COLLISION_HOURS = 24

# Travel-agent exclusion, kept for parity with ops/unpaid_reminders.py.
# NOTE: Leads carry no travel-agent flag today (TA lives on Reservation), so this
# is currently a no-op for the lead-stage nudge. Documented in the plan.
EXCLUDE_TRAVEL_AGENT = True

# Offer variant keys — these are also the FollowUpSequence.segment values used to
# look up the editable templates (seeded in migration 0005).
VARIANT_DISCOUNT = "pre_pickup_discount"
VARIANT_CRUISE = "pre_pickup_cruise_urgency"
VARIANT_URGENCY = "pre_pickup_urgency"


# ── Offer resolution (module-level for easy testing) ───────────────────────────

def get_pre_pickup_discount(lead) -> Decimal:
    """
    Thin seam: today returns the backend-set per-lead discount. A future
    rules-based layer (e.g. derive a discount from how full the date is, with a
    hard margin floor and cap) can slot in HERE without touching the resolver.
    Do NOT build the rules layer now.
    """
    return lead.pre_pickup_discount or Decimal("0")


def resolve_pre_pickup_offer(lead, booking_link):
    """
    Pick the nudge variant + the renderer ``extra`` context. First match wins.
    Returns ``(variant, ctx)``.
    """
    discount = get_pre_pickup_discount(lead)
    if discount and discount > 0:
        # Discount routes to a human; ctx carries the amount for the ops task.
        return VARIANT_DISCOUNT, {"discount": discount}

    ctx = {"booking_link": booking_link}
    if lead.segment == "cruise_transfer":
        return VARIANT_CRUISE, ctx
    return VARIANT_URGENCY, ctx


# ── Result accounting ──────────────────────────────────────────────────────────

@dataclass
class NudgeResult:
    sent: int = 0
    routed_to_human: int = 0
    email_fallback: int = 0
    failed: int = 0
    skipped: dict = field(default_factory=lambda: defaultdict(int))
    actions: list = field(default_factory=list)  # for --dry-run reporting

    def summary_line(self) -> str:
        if not (self.sent or self.routed_to_human or self.email_fallback or self.failed):
            return ""
        return (
            f"Pre-pickup nudges: sent={self.sent}, routed_to_human={self.routed_to_human}, "
            f"email_fallback={self.email_fallback}, failed={self.failed}"
        )


# ── Engine ──────────────────────────────────────────────────────────────────────

class PrePickupNudgeEngine:
    """
    Processes all eligible warm-but-unbooked leads for the current moment.

    Construct fresh per cycle so ``self.now`` is consistent throughout one
    invocation. Tests use ``PrePickupNudgeEngine(now=fixed_now, dry_run=True)``.
    """

    def __init__(self, now=None, dry_run: bool = False):
        self.now = now or timezone.now()
        self.dry_run = dry_run
        self.result = NudgeResult()
        self._service = None  # lazy GHL service

    # ── Public API ──────────────────────────────────────────────────────────

    def process(self) -> NudgeResult:
        """Walk all eligible leads and act on each."""
        for lead in self._candidate_queryset():
            try:
                self.process_one(lead)
            except Exception as exc:
                logger.exception(f"Pre-pickup nudge error on lead {lead.id}: {exc}")
        line = self.result.summary_line()
        if line:
            logger.info(line)
        return self.result

    def send_manual(self, lead, override_message=None) -> str:
        """
        Deliberate, human-triggered nudge (e.g. from the leads board) to fill a
        slow day. Bypasses the date window / 48h throttle / forward-collision
        guards — the operator chose this lead on purpose — but keeps every HARD
        guard: opt-out, converted/lost, already-nudged, the 8am-9pm send window,
        and a contact channel. ``override_message`` (an operator-edited body) is
        sent verbatim for the SMS variants. Returns process_one's action strings.
        """
        from reservations.models import Lead
        from .models import FollowUpTask
        from .timing import is_within_send_window

        if lead.sms_opt_out:
            return self._skip(lead, "sms_opted_out")
        if lead.converted or lead.status in (
            Lead.StatusChoices.CONVERTED, Lead.StatusChoices.LOST
        ):
            return self._skip(lead, "converted_or_lost")
        if self._has_booked_sibling(lead):
            return self._skip(lead, "sibling_booked")
        if FollowUpTask.objects.filter(lead=lead, step_number=NUDGE_STEP).exists():
            return self._skip(lead, "already_nudged")
        if not lead.phone and not lead.email:
            return self._skip(lead, "no_contact")

        booking_link = self._booking_link(lead)
        variant, ctx = resolve_pre_pickup_offer(lead, booking_link)
        if variant == VARIANT_DISCOUNT:
            return self._route_discount_to_human(lead, ctx.get("discount"))
        if not is_within_send_window(self.now):
            return self._skip(lead, "outside_send_window")
        return self._send_nudge(lead, variant, ctx, message=override_message)

    def process_one(self, lead) -> str | None:
        """
        Process a single lead. Returns the action taken
        ("sent:<variant>" / "routed_to_human:discount" / "email_fallback:<variant>"
        / "failed:<variant>" / "skipped:<reason>" / None). Public so the
        management command can target one lead by id.
        """
        action = self._classify_and_act(lead)
        self.result.actions.append(
            {
                "lead_id": lead.id,
                "name": f"{lead.first_name} {lead.last_name}".strip() or f"Lead #{lead.id}",
                "pickup_date": lead.pickup_date.isoformat() if lead.pickup_date else "",
                "action": action or "no_op",
            }
        )
        return action

    # ── Candidate selection ───────────────────────────────────────────────────

    def _candidate_queryset(self):
        from reservations.models import Lead

        today = timezone.localdate(self.now)
        return (
            Lead.objects.filter(
                pickup_date__gte=today + timedelta(days=PICKUP_DAYS_MIN),
                pickup_date__lte=today + timedelta(days=PICKUP_DAYS_MAX),
            )
            .exclude(status__in=[Lead.StatusChoices.CONVERTED, Lead.StatusChoices.LOST])
            .exclude(converted=True)
            .exclude(sms_opt_out=True)  # never text an opted-out number
            .exclude(follow_up_tasks__step_number=NUDGE_STEP)  # "one nudge ever"
            .select_related("vehicle")
            .distinct()
        )

    # ── Per-lead classification ───────────────────────────────────────────────

    def _classify_and_act(self, lead) -> str | None:
        from reservations.models import Lead
        from .models import FollowUpTask
        from .timing import is_within_send_window

        today = timezone.localdate(self.now)

        # Defensive guards (also make process_one safe for ad-hoc / single-lead).
        if not lead.pickup_date:
            return self._skip(lead, "no_pickup_date")
        days = (lead.pickup_date - today).days
        if days < PICKUP_DAYS_MIN or days > PICKUP_DAYS_MAX:
            return self._skip(lead, "outside_window")
        if lead.pickup_date <= today:
            return self._skip(lead, "pickup_passed")  # never same-day / past
        if lead.converted or lead.status in (
            Lead.StatusChoices.CONVERTED, Lead.StatusChoices.LOST
        ):
            return self._skip(lead, "converted_or_lost")
        # Duplicate-lead safety net: never nudge someone who already booked under a
        # TWIN lead. A round-trip + one-way quote create two leads, and booking
        # converts only ONE (auto_convert_lead_on_reservation uses .first()), so the
        # still-"interested" twin would otherwise be texted even though the customer
        # already booked — the exact trust-eroding case this guard prevents.
        if self._has_booked_sibling(lead):
            return self._skip(lead, "sibling_booked")
        # Opt-out (TCPA): never text an opted-out number. The shared send_sms path
        # also blocks this, but skip early so we don't fall through to the email
        # fallback for an SMS-opted-out lead.
        if lead.sms_opt_out:
            return self._skip(lead, "sms_opted_out")
        # NOTE: we intentionally do NOT suppress on has_replied / needs_human_follow_up
        # — replied-but-unbooked leads are the core target (owner decision).
        if EXCLUDE_TRAVEL_AGENT and getattr(lead, "is_travel_agent", False):
            return self._skip(lead, "travel_agent")  # inert today: no lead TA flag

        # Already nudged? (race-safe re-check; the SQL exclude also covers this.)
        if FollowUpTask.objects.filter(lead=lead, step_number=NUDGE_STEP).exists():
            return self._skip(lead, "already_nudged")

        # Phone-level dedup: a person may have multiple leads (e.g. round-trip
        # outbound + return quotes). If ANY lead sharing this phone was already
        # nudged, skip — mirrors the existing sequence's anti-double-text guards
        # (ghl_integration/tasks.py). Within one run, the first lead claims a
        # step-6 row and later same-phone leads see it here.
        if lead.normalized_phone and FollowUpTask.objects.filter(
            step_number=NUDGE_STEP, lead__normalized_phone=lead.normalized_phone
        ).exclude(lead_id=lead.id).exists():
            return self._skip(lead, "phone_already_nudged")

        # Adjacency throttle — any recent outbound.
        last_out = self._last_outbound_at(lead)
        if last_out is not None and (self.now - last_out) < timedelta(
            hours=MIN_OUTBOUND_GAP_HOURS
        ):
            return self._skip(lead, "recent_outbound")

        # Forward-collision — a form-sequence step due very soon.
        if self._has_imminent_followup(lead):
            return self._skip(lead, "imminent_followup")

        # Channel check.
        if not lead.phone and not lead.email:
            return self._skip(lead, "no_contact")

        # Resolve the offer.
        booking_link = self._booking_link(lead)
        variant, ctx = resolve_pre_pickup_offer(lead, booking_link)

        # Discount → route to a human; never auto-send a price. Not gated on the
        # send window (it creates a task, not an SMS).
        if variant == VARIANT_DISCOUNT:
            return self._route_discount_to_human(lead, ctx.get("discount"))

        # Send window — defer (write nothing) if outside 8 AM–9 PM ET.
        if not is_within_send_window(self.now):
            return self._skip(lead, "outside_send_window")

        return self._send_nudge(lead, variant, ctx)

    # ── Actions ───────────────────────────────────────────────────────────────

    def render_preview(self, lead):
        """
        Resolve the offer + render the message WITHOUT sending, so a human can
        review/edit it first. Returns (variant, message). ``message`` is None for
        the discount variant (it routes to a human, not an SMS) or if no template.
        """
        from .models import FollowUpSequence
        from .templates_engine import render_follow_up_message

        booking_link = self._booking_link(lead)
        variant, ctx = resolve_pre_pickup_offer(lead, booking_link)
        if variant == VARIANT_DISCOUNT:
            return variant, None
        row = FollowUpSequence.objects.filter(
            step_number=NUDGE_STEP, segment=variant, is_active=True
        ).first()
        if not row:
            return variant, None
        return variant, render_follow_up_message(row.message_template, lead, extra=ctx)

    def _send_nudge(self, lead, variant, ctx, message=None) -> str:
        from .models import FollowUpSequence, FollowUpTask
        from .templates_engine import render_follow_up_message

        # message=None → render from the template; a non-None message is an
        # operator-edited override from the leads board.
        if message is None:
            template_row = FollowUpSequence.objects.filter(
                step_number=NUDGE_STEP, segment=variant, is_active=True
            ).first()
            if not template_row:
                logger.warning(f"No active pre-pickup template for variant '{variant}'")
                return self._skip(lead, "no_template")
            message = render_follow_up_message(template_row.message_template, lead, extra=ctx)

        if self.dry_run:
            self.result.sent += 1
            return f"sent:{variant}"

        # Claim the single nudge slot before sending (idempotent across the
        # scheduler + manual command; the (lead, step) unique key is the guard).
        task = self._claim(lead, variant)
        if task is None:
            return self._skip(lead, "already_nudged")

        sms_ok = False
        if lead.phone:
            contact_id = self._ensure_contact(lead)
            if contact_id:
                try:
                    sms_ok = self.service.send_sms(contact_id, message)
                except Exception as e:
                    logger.error(f"Pre-pickup SMS error for lead #{lead.id}: {e}")
                    sms_ok = False

        if sms_ok:
            self._finalize(task, FollowUpTask.StatusChoices.SENT, message)
            self._touch_contact(lead)
            self._log_activity(lead, variant, channel="sms")
            self.result.sent += 1
            time_module.sleep(1)  # rate limit, mirrors process_follow_up_batch
            return f"sent:{variant}"

        # SMS failed (or no phone) → existing quote-email fallback.
        from .tasks import _try_email_fallback

        if lead.email and _try_email_fallback(lead):
            self._set_lead_flags(lead, needs_human_follow_up=True)
            self._finalize(
                task, FollowUpTask.StatusChoices.SENT,
                "(SMS failed — quote email fallback sent; flagged for human follow-up)",
            )
            self._log_activity(lead, variant, channel="email")
            self.result.email_fallback += 1
            return f"email_fallback:{variant}"

        # Both channels failed.
        self._finalize(task, FollowUpTask.StatusChoices.FAILED, "(SMS and email both failed)")
        self.result.failed += 1
        return f"failed:{variant}"

    def _route_discount_to_human(self, lead, discount) -> str:
        from .models import FollowUpTask

        if self.dry_run:
            self.result.routed_to_human += 1
            return "routed_to_human:discount"

        task = self._claim(lead, VARIANT_DISCOUNT)
        if task is None:
            return self._skip(lead, "already_nudged")

        self._set_lead_flags(lead, needs_human_follow_up=True)
        self._create_discount_task(lead, discount)
        amount = f"${discount:,.0f}" if discount else "discount"
        self._finalize(
            task, FollowUpTask.StatusChoices.SKIPPED,
            f"Routed to human for {amount} pre-pickup discount (no automated SMS).",
        )
        self.result.routed_to_human += 1
        return "routed_to_human:discount"

    # ── Helpers ───────────────────────────────────────────────────────────────

    @property
    def service(self):
        if self._service is None:
            from .services import GoHighLevelService
            self._service = GoHighLevelService()
        return self._service

    def _skip(self, lead, reason) -> str:
        self.result.skipped[reason] += 1
        return f"skipped:{reason}"

    def _has_booked_sibling(self, lead) -> bool:
        """
        True if a DIFFERENT lead sharing this person's phone OR email has already
        booked (converted). This is the duplicate-lead safety net: it does NOT
        rely on the duplicate twins being merged or converted together — a single
        converted lead anywhere on the same phone/email suppresses the nudge. Only
        ever suppresses a send, so it cannot cause an erroneous text. Mirrors the
        phone_already_nudged sibling guard.
        """
        from django.db.models import Q
        from reservations.models import Lead

        ident = Q()
        if lead.normalized_phone:
            ident |= Q(normalized_phone=lead.normalized_phone)
        if lead.email:
            ident |= Q(email__iexact=lead.email)
        if not ident:
            return False
        return (
            Lead.objects.filter(ident)
            .filter(Q(converted=True) | Q(status=Lead.StatusChoices.CONVERTED))
            .exclude(pk=lead.pk)
            .exists()
        )

    def _last_outbound_at(self, lead):
        """Most recent outbound to the lead across locally-visible signals."""
        from .models import FollowUpTask

        last_task = (
            FollowUpTask.objects.filter(
                lead=lead, status=FollowUpTask.StatusChoices.SENT, sent_at__isnull=False
            )
            .order_by("-sent_at")
            .values_list("sent_at", flat=True)
            .first()
        )
        candidates = [
            lead.last_contact_date,
            lead.initial_sms_sent_at,
            lead.initial_email_sent_at,
            last_task,
        ]
        ts = [t for t in candidates if t is not None]
        return max(ts) if ts else None

    def _has_imminent_followup(self, lead) -> bool:
        from .models import FollowUpTask

        horizon = self.now + timedelta(hours=FORWARD_COLLISION_HOURS)
        return FollowUpTask.objects.filter(
            lead=lead,
            status=FollowUpTask.StatusChoices.PENDING,
            step_number__lte=5,
            scheduled_at__lte=horizon,
        ).exists()

    def _booking_link(self, lead) -> str:
        from users.emails import GENERIC_BOOKING_URL, resolve_booking_url

        try:
            return resolve_booking_url(lead)
        except Exception:
            return GENERIC_BOOKING_URL

    def _claim(self, lead, variant):
        """
        Atomically claim the single nudge slot. Returns the FollowUpTask if this
        call created it, or None if it already existed (already nudged/claimed).
        The transient PENDING step-6 row is invisible to process_follow_up_batch
        (scoped to step ≤ 5).
        """
        from .models import FollowUpTask

        now = timezone.now()
        try:
            task, created = FollowUpTask.objects.get_or_create(
                lead=lead,
                step_number=NUDGE_STEP,
                defaults={
                    "segment": variant,
                    "status": FollowUpTask.StatusChoices.PENDING,
                    "scheduled_at": now,
                },
            )
        except IntegrityError:
            return None
        return task if created else None

    def _finalize(self, task, status, body):
        from .models import FollowUpTask

        task.status = status
        task.message_body = body
        if status == FollowUpTask.StatusChoices.SENT:
            task.sent_at = timezone.now()
        task.save(update_fields=["status", "message_body", "sent_at"])

    def _ensure_contact(self, lead):
        if lead.ghl_contact_id:
            return lead.ghl_contact_id
        try:
            contact_id = self.service.create_or_update_contact(lead)
        except Exception as e:
            logger.error(f"Failed to create GHL contact for lead #{lead.id}: {e}")
            return None
        if contact_id:
            lead.ghl_contact_id = contact_id
            lead.ghl_synced_at = timezone.now()
            lead.save(update_fields=["ghl_contact_id", "ghl_synced_at"])
        return contact_id

    def _touch_contact(self, lead):
        lead.last_contact_date = timezone.now()
        lead.contact_attempts = (lead.contact_attempts or 0) + 1
        lead.save(update_fields=["last_contact_date", "contact_attempts"])

    def _set_lead_flags(self, lead, **flags):
        # save(update_fields=...) is safe: Lead.save() recomputes normalized_phone
        # on the instance but only the listed columns are persisted.
        for k, v in flags.items():
            setattr(lead, k, v)
        lead.save(update_fields=list(flags.keys()))

    def _create_discount_task(self, lead, discount):
        from ops.models import OperationalTask
        from ops.services import create_task

        amount = f"${discount:,.0f}" if discount else "a"

        # SLA: due on the lead's pickup day (9 AM ET) so it sorts onto the right
        # ops-board day, and escalate a day before pickup so it can't slip.
        # pickup_date is date-only; make_aware uses the active tz (settings
        # TIME_ZONE = America/New_York). With no pickup_date, create_task defaults
        # due_at to now().
        due_at = None
        escalate_at = None
        if lead.pickup_date:
            due_at = timezone.make_aware(datetime.combine(lead.pickup_date, dt_time(9, 0)))
            escalate_at = due_at - timedelta(days=1)

        create_task(
            task_type=OperationalTask.TaskType.MANUAL,
            title=f"Apply {amount} pre-pickup discount & book Lead #{lead.id}",
            description=(
                f"Lead #{lead.id} ({lead.first_name}) has a backend-set pre-pickup "
                f"discount of ${discount}. Trip is on {lead.pickup_date}. The booking "
                f"flow can't auto-apply a discount, so book them manually with the "
                f"discount applied (or issue a Stripe promo code). No automated "
                f"discount SMS was sent."
            ),
            priority=OperationalTask.Priority.HIGH,
            due_at=due_at,
            escalate_at=escalate_at,
            lead=lead,
            metadata={
                "kind": "pre_pickup_discount",
                "discount_amount": str(discount),
                "pickup_date": lead.pickup_date.isoformat() if lead.pickup_date else None,
                "source": "pre_pickup_nudge",
            },
        )

    def _log_activity(self, lead, variant, channel):
        from .models import LeadActivity

        try:
            LeadActivity.objects.create(
                lead=lead,
                activity_type=LeadActivity.ActivityType.SMS_SENT,
                description=f"Pre-pickup nudge sent ({variant}, {channel})",
                metadata={"step": NUDGE_STEP, "variant": variant, "channel": channel},
            )
        except Exception:
            pass


def send_pre_pickup_nudges():
    """Module-level entry point for the scheduler. Returns a summary dict."""
    result = PrePickupNudgeEngine().process()
    return {
        "sent": result.sent,
        "routed_to_human": result.routed_to_human,
        "email_fallback": result.email_fallback,
        "failed": result.failed,
    }
