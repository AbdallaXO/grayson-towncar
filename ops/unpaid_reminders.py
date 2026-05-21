"""
Automated payment-reminder engine for unpaid reservations.

Lifecycle per reservation:

    booking ─── +2h ─── +24h ─── ··· ─── pickup-3d ─── pickup-24h ─── pickup-2h ─── pickup
                 │       │                   │             │              │
              first    second           three_day        final       auto_cancel_flag
              email    email             email          email       (no email; flag + escalate)

The engine is invoked every scheduler cycle from ops.tasks.generate_ops_tasks.
Each call walks eligible reservations, applies exclusion guards in order, and
performs at most one stage action per reservation per cycle.

Idempotency: every stage has a dedicated ``unpaid_*_sent_at`` timestamp on
Reservation. The queryset filters those for IS NULL and the timestamp is set
only after a successful synchronous send, so retries cannot double-send.

Toggles:
- EXCLUDE_TRAVEL_AGENT: flip to False to start sending reminders to
  travel-agent bookings as well. Lives at module top so the change is a
  one-line code edit reviewed in a PR.
- RECENT_STAFF_CONTACT_HOURS: suppression window for "staff just talked to
  this guest" (defers the cycle, doesn't undo).
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.db.models import Q
from django.urls import reverse
from django.utils import timezone

from ops.models import CommunicationAttempt, OperationalTask
from ops.services import create_task
from reservations.models import Reservation
from users.emails import send_payment_reminder

logger = logging.getLogger(__name__)


# ── Toggles ─────────────────────────────────────────────────────────────────

EXCLUDE_TRAVEL_AGENT = True
RECENT_STAFF_CONTACT_HOURS = 6


# ── Stage definitions ───────────────────────────────────────────────────────

STAGE_FIRST = "first"
STAGE_SECOND = "second"
STAGE_THREE_DAY = "three_day"
STAGE_FINAL = "final"
STAGE_AUTO_CANCEL_FLAG = "auto_cancel_flag"

STAGE_FIELD = {
    STAGE_FIRST: "unpaid_first_reminder_sent_at",
    STAGE_SECOND: "unpaid_second_reminder_sent_at",
    STAGE_THREE_DAY: "unpaid_three_day_warning_sent_at",
    STAGE_FINAL: "unpaid_final_warning_sent_at",
}

# Stages that send an email.
EMAIL_STAGES = (STAGE_FIRST, STAGE_SECOND, STAGE_THREE_DAY, STAGE_FINAL)


# ── Result accounting ──────────────────────────────────────────────────────


@dataclass
class ReminderResult:
    sent: dict = field(default_factory=lambda: defaultdict(int))
    flagged_for_cancel: int = 0
    dup_blocked: int = 0
    skipped: dict = field(default_factory=lambda: defaultdict(int))
    actions: list = field(default_factory=list)  # for --dry-run reporting

    def total_sent(self) -> int:
        return sum(self.sent.values())

    def summary_line(self) -> str:
        if self.total_sent() == 0 and self.flagged_for_cancel == 0:
            # Quiet log when nothing happened.
            return ""
        sent_parts = ", ".join(
            f"{stage}={count}" for stage, count in self.sent.items() if count
        ) or "0"
        return (
            f"Unpaid reminders: sent={self.total_sent()} ({sent_parts}), "
            f"flagged_for_cancel={self.flagged_for_cancel}, "
            f"dup_blocked={self.dup_blocked}"
        )


# ── Engine ──────────────────────────────────────────────────────────────────


class UnpaidReminderEngine:
    """
    Processes all eligible unpaid reservations for the current moment.

    Construct fresh per cycle so ``self.now`` is consistent throughout one
    invocation. Tests use ``UnpaidReminderEngine(now=fixed_now, dry_run=True)``.
    """

    def __init__(self, now=None, dry_run: bool = False):
        self.now = now or timezone.now()
        self.dry_run = dry_run
        self.result = ReminderResult()
        self._duplicate_cache: dict | None = None

    # ── Public API ────────────────────────────────────────────────────────

    def process(self) -> ReminderResult:
        """Walk all eligible reservations and act on each."""
        for reservation in self._candidate_queryset():
            try:
                self.process_one(reservation)
            except Exception as exc:
                logger.exception(
                    f"Reminder engine error on reservation {reservation.id}: {exc}"
                )
        if self.result.summary_line():
            logger.info(self.result.summary_line())
        return self.result

    def process_one(self, reservation: Reservation) -> str | None:
        """
        Process a single reservation. Returns the action taken
        ("sent:first" / "flagged" / "skipped:<reason>" / "dup_blocked" / None).
        Public so the management command can target one reservation by uuid.
        """
        action = self._classify_and_act(reservation)
        self.result.actions.append(
            {
                "reservation_id": reservation.id,
                "uuid": str(reservation.uuid),
                "customer": (
                    reservation.customer.get_full_name()
                    if reservation.customer
                    else "(no customer)"
                ),
                "action": action or "no_op",
            }
        )
        return action

    # ── Queryset ──────────────────────────────────────────────────────────

    def _candidate_queryset(self):
        """
        Coarse SQL pre-filter. Final exclusions run in Python because they
        depend on cached_property logic (payment_status, first_pickup_dt)
        that the existing _scan_unpaid_reservations also handles in Python.
        """
        # Pickup horizon: anything from now back to "booked >= 2h ago" with
        # pickup not yet past, up to 14 days out. Reservations whose pickup
        # has fully passed can't be reminded anyway.
        booking_cutoff = self.now - timedelta(hours=2)
        pickup_horizon = (self.now + timedelta(days=14)).date()
        today = timezone.localdate(self.now)

        qs = (
            Reservation.objects.filter(
                # At least one leg with an upcoming or today pickup.
                legs__pickup_date__gte=today,
                legs__pickup_date__lte=pickup_horizon,
                # Not cancelled.
                # Booked at least 2h ago — even the earliest reminder can't
                # fire any sooner.
                created_at__lte=booking_cutoff,
                # Not on staff hold.
                unpaid_auto_reminder_hold=False,
                # Not already flagged as suspected duplicate.
                unpaid_duplicate_suspected=False,
                # Has unsent SOMETHING (any stage_field NULL OR auto-cancel
                # flag NULL). Use a Q-OR; cheap because the indexes exist.
            )
            .exclude(status="cancelled")
            .exclude(is_paid=True)
            .filter(
                Q(unpaid_first_reminder_sent_at__isnull=True)
                | Q(unpaid_second_reminder_sent_at__isnull=True)
                | Q(unpaid_three_day_warning_sent_at__isnull=True)
                | Q(unpaid_final_warning_sent_at__isnull=True)
                | Q(unpaid_auto_cancel_eligible_at__isnull=True)
            )
            .select_related("customer", "travel_agent")
            .prefetch_related("payments", "legs")
            .distinct()
        )
        if EXCLUDE_TRAVEL_AGENT:
            qs = qs.filter(travel_agent__isnull=True)
        return qs

    # ── Per-reservation classification ────────────────────────────────────

    def _classify_and_act(self, reservation: Reservation) -> str | None:
        # 1. Cancelled
        if reservation.status == "cancelled":
            return self._skip(reservation, "cancelled")

        # 2. Paid or card_saved (save-card finalized)
        ps = reservation.payment_status
        if ps in ("paid", "card_saved"):
            return self._skip(reservation, f"payment_status={ps}")

        # 3. Zero balance (defensive)
        if reservation.amount_owed <= Decimal("0.01"):
            return self._skip(reservation, "amount_owed<=0")

        # 4. Travel agent (toggle gate; queryset already filters but re-check
        #    so process_one() works for ad-hoc / dry-run calls too)
        if EXCLUDE_TRAVEL_AGENT and reservation.travel_agent_id is not None:
            return self._skip(reservation, "travel_agent")

        # 5. Manual hold
        if reservation.unpaid_auto_reminder_hold:
            return self._skip(reservation, "manual_hold")

        # 6. Contact info present
        if not reservation.customer or not reservation.customer.email:
            return self._skip(reservation, "no_email")

        # 7. Already flagged duplicate
        if reservation.unpaid_duplicate_suspected:
            return self._skip(reservation, "duplicate_suspected")

        # 8. Pickup must exist and not be in the past
        pickup_dt = reservation.first_pickup_dt
        if pickup_dt is None:
            return self._skip(reservation, "no_active_leg")
        if pickup_dt <= self.now:
            return self._skip(reservation, "pickup_passed")

        # 9. Recent staff contact suppression — defer one cycle
        if self._has_recent_staff_contact(reservation):
            return self._skip(reservation, "recent_staff_contact")

        # 10. Live duplicate scan
        if self._is_duplicate(reservation):
            self._handle_duplicate(reservation)
            return "dup_blocked"

        # Pick the stage whose window contains `now`.
        stage = self._pick_stage(reservation, pickup_dt)
        if stage is None:
            return self._skip(reservation, "no_stage_window")

        if stage == STAGE_AUTO_CANCEL_FLAG:
            self._flag_for_auto_cancel(reservation)
            return "flagged"

        # Email stage
        self._send_stage_email(reservation, stage)
        return f"sent:{stage}"

    def _pick_stage(self, reservation: Reservation, pickup_dt) -> str | None:
        """
        Returns the first stage whose window contains ``self.now`` and which
        hasn't already been sent. ``None`` if no stage applies right now.
        """
        booking_dt = reservation.created_at
        now = self.now
        time_to_pickup = pickup_dt - now

        # Stage 5 first: pickup <= 2h away → flag, never send.
        if (
            time_to_pickup <= timedelta(hours=2)
            and reservation.unpaid_auto_cancel_eligible_at is None
        ):
            return STAGE_AUTO_CANCEL_FLAG

        # Stage 4: final 24h reminder window (pickup-24h .. pickup-2h)
        if (
            timedelta(hours=2) < time_to_pickup <= timedelta(hours=24)
            and reservation.unpaid_final_warning_sent_at is None
        ):
            return STAGE_FINAL

        # Stage 3: three-day warning window (pickup-3d .. pickup-24h)
        if (
            timedelta(hours=24) < time_to_pickup <= timedelta(days=3)
            and reservation.unpaid_three_day_warning_sent_at is None
        ):
            return STAGE_THREE_DAY

        # Stages 1 & 2: booking-relative — only fire if pickup is still
        # more than 24h away (don't double up on the near-pickup reminders).
        if time_to_pickup > timedelta(hours=24):
            since_booking = now - booking_dt
            # Stage 2: 24h after booking, gated on stage 1
            if (
                since_booking >= timedelta(hours=24)
                and reservation.unpaid_second_reminder_sent_at is None
                and reservation.unpaid_first_reminder_sent_at is not None
            ):
                return STAGE_SECOND
            # Stage 1: 2h after booking
            if (
                since_booking >= timedelta(hours=2)
                and reservation.unpaid_first_reminder_sent_at is None
            ):
                return STAGE_FIRST

        return None

    # ── Side effects ──────────────────────────────────────────────────────

    def _send_stage_email(self, reservation: Reservation, stage: str) -> None:
        if self.dry_run:
            self.result.sent[stage] += 1
            return

        checkout_url = (
            f"{settings.SITE_BASE_URL}"
            f"{reverse('create_checkout_session', args=[str(reservation.uuid)])}"
        )

        try:
            send_payment_reminder(
                reservation=reservation,
                checkout_url=checkout_url,
                stage=stage,
                sent_by=None,
                automated=True,
            )
        except Exception as exc:
            logger.exception(
                f"Reminder send failed for reservation {reservation.id} "
                f"stage={stage}: {exc}"
            )
            return  # leave sent_at NULL — next cycle will retry

        # Persist the sent_at timestamp via UPDATE so we don't trigger
        # auto_now on updated_at (keeps simple_history quiet).
        field_name = STAGE_FIELD[stage]
        Reservation.objects.filter(pk=reservation.pk).update(
            **{field_name: self.now}
        )
        setattr(reservation, field_name, self.now)
        self.result.sent[stage] += 1

    def _flag_for_auto_cancel(self, reservation: Reservation) -> None:
        self.result.flagged_for_cancel += 1
        if self.dry_run:
            return

        Reservation.objects.filter(pk=reservation.pk).update(
            unpaid_auto_cancel_eligible_at=self.now,
        )
        reservation.unpaid_auto_cancel_eligible_at = self.now

        # Escalate the open PAYMENT_CHASE task (or create one if none exists).
        existing = OperationalTask.objects.filter(
            task_type=OperationalTask.TaskType.PAYMENT_CHASE,
            reservation=reservation,
            status__in=list(OperationalTask.OPEN_STATUSES),
        ).first()

        urgent_title = (
            f"URGENT: pickup in <2h, unpaid ${reservation.amount_owed} — "
            f"{reservation.customer.get_full_name()}"
        )
        urgent_desc = (
            "Reservation crossed the pickup-2h threshold still unpaid. "
            "Auto-cancellation is disabled in v1 — please cancel manually "
            "or contact the guest. Marked as auto_cancel_eligible_at = "
            f"{self.now:%Y-%m-%d %H:%M %Z}."
        )

        if existing:
            existing.status = OperationalTask.Status.ESCALATED
            existing.priority = OperationalTask.Priority.CRITICAL
            existing.title = urgent_title
            existing.description = (
                (existing.description + "\n\n" if existing.description else "")
                + urgent_desc
            )
            existing.due_at = self.now
            existing.save(
                update_fields=[
                    "status", "priority", "title", "description",
                    "due_at", "updated_at",
                ]
            )
            logger.warning(
                f"Escalated PAYMENT_CHASE #{existing.id} to CRITICAL "
                f"for reservation {reservation.id} (T-2h flag)"
            )
        else:
            # No existing task — create one so staff sees it.
            create_task(
                task_type=OperationalTask.TaskType.PAYMENT_CHASE,
                title=urgent_title,
                description=urgent_desc,
                due_at=self.now,
                priority=OperationalTask.Priority.CRITICAL,
                reservation=reservation,
                metadata={
                    "trigger": "auto_cancel_flag",
                    "amount_owed": str(reservation.amount_owed),
                    "automated": True,
                },
            )

    # ── Duplicate detection ───────────────────────────────────────────────

    def _build_duplicate_cache(self):
        """
        Build a one-shot index keyed by (last_name_lower, phone_last10,
        pickup_date) → list[Reservation]. Mirrors dispatching.views.
        duplicate_reservations grouping.
        """
        if self._duplicate_cache is not None:
            return self._duplicate_cache

        today = timezone.localdate(self.now)
        cutoff = today - timedelta(days=90)

        reservations = (
            Reservation.objects.filter(legs__pickup_date__gte=cutoff)
            .exclude(status="cancelled")
            .select_related("customer")
            .prefetch_related("legs")
            .distinct()
        )

        groups: dict[tuple, list[Reservation]] = defaultdict(list)
        for res in reservations:
            customer = res.customer
            if not customer:
                continue
            first_leg = next(iter(res.legs.all()), None)
            if not first_leg:
                continue
            phone_digits = "".join(
                ch for ch in (customer.phone_number or "") if ch.isdigit()
            )[-10:]
            if not phone_digits:
                continue
            name_part = (
                customer.last_name or customer.first_name or ""
            ).strip().lower()
            if not name_part:
                continue
            key = (name_part, phone_digits, first_leg.pickup_date)
            groups[key].append(res)

        self._duplicate_cache = groups
        return groups

    def _is_duplicate(self, reservation: Reservation) -> bool:
        customer = reservation.customer
        if not customer:
            return False
        first_leg = (
            reservation.legs.exclude(status="cancelled")
            .order_by("pickup_date", "pickup_time")
            .first()
        )
        if not first_leg:
            return False
        phone_digits = "".join(
            ch for ch in (customer.phone_number or "") if ch.isdigit()
        )[-10:]
        if not phone_digits:
            return False
        name_part = (
            customer.last_name or customer.first_name or ""
        ).strip().lower()
        if not name_part:
            return False

        key = (name_part, phone_digits, first_leg.pickup_date)
        groups = self._build_duplicate_cache()
        bucket = groups.get(key, [])
        # Dedup by pk in case prefetch returned doubles.
        unique_ids = {r.pk for r in bucket}
        return len(unique_ids) >= 2 and reservation.pk in unique_ids

    def _handle_duplicate(self, reservation: Reservation) -> None:
        self.result.dup_blocked += 1
        logger.info(
            f"Skipped reservation {reservation.id}: suspected duplicate "
            f"(name+phone+pickup_date matches another live reservation)"
        )
        if self.dry_run:
            return

        Reservation.objects.filter(pk=reservation.pk).update(
            unpaid_duplicate_suspected=True,
        )
        reservation.unpaid_duplicate_suspected = True

        create_task(
            task_type=OperationalTask.TaskType.PAYMENT_CHASE,
            title=(
                f"Possible duplicate reservation #{reservation.id} — "
                f"{reservation.customer.get_full_name()}"
            ),
            description=(
                "The unpaid-reminder engine flagged this reservation as a "
                "possible duplicate (same last name + phone last-10 + pickup "
                "date as another live reservation). Reminders are paused. "
                "Resolve via /duplicate-reservations/, then clear "
                "unpaid_duplicate_suspected to re-enable reminders."
            ),
            due_at=self.now,
            priority=OperationalTask.Priority.HIGH,
            reservation=reservation,
            metadata={
                "trigger": "duplicate_suspected",
                "automated": True,
            },
        )

    # ── Helpers ───────────────────────────────────────────────────────────

    def _has_recent_staff_contact(self, reservation: Reservation) -> bool:
        if RECENT_STAFF_CONTACT_HOURS <= 0:
            return False
        cutoff = self.now - timedelta(hours=RECENT_STAFF_CONTACT_HOURS)
        return CommunicationAttempt.objects.filter(
            task__reservation=reservation,
            task__task_type=OperationalTask.TaskType.PAYMENT_CHASE,
            staff_user__isnull=False,
            created_at__gte=cutoff,
        ).exists()

    def _skip(self, reservation: Reservation, reason: str) -> str:
        self.result.skipped[reason] += 1
        logger.info(f"Skipped reservation {reservation.id}: {reason}")
        return f"skipped:{reason}"
