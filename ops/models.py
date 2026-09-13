"""
Operational task queue models for Grayson Towncar staff productivity layer.

Three models:
- OperationalTask: unified work-queue item for all operational task types
- CommunicationAttempt: tracks every outbound contact attempt tied to a task
- StaffActivity: passive tracking for owner visibility into staff behavior
"""

from datetime import time

from django.conf import settings
from django.db import models
from django.utils import timezone


class OperationalTask(models.Model):
    """
    Central work-queue item. Covers unpaid reservations, flight verification,
    driver assignment, contact form follow-up, and manual tasks.

    Uses concrete FKs (not GenericFK) because the related object types are a
    closed set — matching how FollowUpTask uses a concrete FK to Lead.
    Priority is SmallIntegerField so ORDER BY priority ASC, due_at ASC gives
    a natural queue ordering where 1=Critical sorts first.
    """

    class TaskType(models.TextChoices):
        PAYMENT_CHASE = "payment_chase", "Unpaid Reservations"
        FLIGHT_VERIFICATION = "flight_verify", "Flight Verification"
        DRIVER_CONFLICT = "driver_conflict", "Driver Conflict"
        DRIVER_ASSIGNMENT = "driver_assign", "Driver Assignment"
        CONFIRMATION_TEXTS = "confirmation_texts", "Confirmation Texts"
        CONTACT_FORM = "contact_form", "Contact Us"
        AFTERHOURS_FEE = "afterhours_fee", "After-Hours Fee"
        TIGHT_TURN = "tight_turn", "Tight Turn"
        MANUAL = "manual", "Manual Task"

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        IN_PROGRESS = "in_progress", "In Progress"
        SNOOZED = "snoozed", "Snoozed"
        COMPLETED = "completed", "Completed"
        CANCELLED = "cancelled", "Cancelled"
        ESCALATED = "escalated", "Escalated"

    class Priority(models.IntegerChoices):
        CRITICAL = 1, "Critical"
        HIGH = 2, "High"
        MEDIUM = 3, "Medium"
        LOW = 4, "Low"

    # ── Identity ──
    task_type = models.CharField(max_length=30, choices=TaskType.choices, db_index=True)
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.PENDING, db_index=True
    )
    priority = models.SmallIntegerField(
        choices=Priority.choices, default=Priority.MEDIUM, db_index=True
    )
    title = models.CharField(max_length=200)
    description = models.TextField(blank=True)

    # ── Related objects (all nullable) ──
    reservation = models.ForeignKey(
        "reservations.Reservation",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="ops_tasks",
    )
    leg = models.ForeignKey(
        "reservations.Leg",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="ops_tasks",
    )
    lead = models.ForeignKey(
        "reservations.Lead",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="ops_tasks",
    )
    contact_form = models.ForeignKey(
        "users.ContactUsForm",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="ops_tasks",
    )

    # ── Assignment ──
    assigned_to = models.ForeignKey(
        "auth.User",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="assigned_ops_tasks",
    )
    assigned_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When the task was last assigned or reassigned",
    )
    created_by = models.ForeignKey(
        "auth.User",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="created_ops_tasks",
    )

    # ── Scheduling ──
    due_at = models.DateTimeField(db_index=True)
    snoozed_until = models.DateTimeField(null=True, blank=True)
    escalate_at = models.DateTimeField(
        null=True, blank=True, help_text="Auto-escalate if still open after this time"
    )

    # ── Retry / follow-up ──
    attempts = models.PositiveSmallIntegerField(default=0)
    max_attempts = models.PositiveSmallIntegerField(default=5)
    last_attempt_at = models.DateTimeField(null=True, blank=True)
    next_retry_at = models.DateTimeField(null=True, blank=True)

    # ── Dependency (soft — used for UI warnings, not hard blocking) ──
    blocked_by = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="blocks",
        help_text="Soft dependency — shows a warning, does not prevent action",
    )

    # ── Resolution ──
    resolved_at = models.DateTimeField(null=True, blank=True)
    resolved_by = models.ForeignKey(
        "auth.User",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="resolved_ops_tasks",
    )
    resolution_notes = models.TextField(blank=True)

    # ── Metadata ──
    metadata = models.JSONField(
        default=dict,
        blank=True,
        help_text="Task-specific data: flight mismatch details, payment amounts, etc.",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["priority", "due_at"]
        indexes = [
            models.Index(
                fields=["status", "priority", "due_at"], name="idx_ops_queue"
            ),
            models.Index(
                fields=["task_type", "status"], name="idx_ops_type_status"
            ),
            models.Index(
                fields=["assigned_to", "status"], name="idx_ops_assigned"
            ),
            models.Index(
                fields=["leg", "task_type", "status"], name="idx_ops_leg_dedup"
            ),
            models.Index(
                fields=["reservation", "task_type", "status"],
                name="idx_ops_res_dedup",
            ),
            models.Index(
                fields=["lead", "task_type", "status"], name="idx_ops_lead_dedup"
            ),
        ]
        verbose_name = "Operational Task"
        verbose_name_plural = "Operational Tasks"

    def __str__(self):
        return f"[{self.get_priority_display()}] {self.title} ({self.get_status_display()})"

    # ── Convenience properties ──

    OPEN_STATUSES = frozenset(
        [Status.PENDING, Status.IN_PROGRESS, Status.SNOOZED, Status.ESCALATED]
    )

    @property
    def is_open(self):
        return self.status in self.OPEN_STATUSES

    @property
    def is_overdue(self):
        if not self.is_open:
            return False
        return self.due_at < timezone.now()

    @property
    def related_object(self):
        """Return the most specific related object for display."""
        return self.leg or self.reservation or self.lead or self.contact_form

    @property
    def customer(self):
        """Convenience: get the customer associated with this task."""
        if self.reservation:
            return self.reservation.customer
        if self.leg:
            return self.leg.reservation.customer
        return None

    # ── Task type display helpers ──

    TASK_TYPE_ICONS = {
        TaskType.PAYMENT_CHASE: "bi-currency-dollar",
        TaskType.FLIGHT_VERIFICATION: "bi-airplane",
        TaskType.DRIVER_CONFLICT: "bi-exclamation-triangle",
        TaskType.DRIVER_ASSIGNMENT: "bi-person-plus",
        TaskType.CONFIRMATION_TEXTS: "bi-chat-text-fill",
        TaskType.CONTACT_FORM: "bi-envelope-paper",
        TaskType.AFTERHOURS_FEE: "bi-moon-stars",
        TaskType.TIGHT_TURN: "bi-hourglass-split",
        TaskType.MANUAL: "bi-pencil-square",
    }

    TASK_TYPE_COLORS = {
        TaskType.PAYMENT_CHASE: "#f39c12",
        TaskType.FLIGHT_VERIFICATION: "#3498db",
        TaskType.DRIVER_CONFLICT: "#e74c3c",
        TaskType.DRIVER_ASSIGNMENT: "#9b59b6",
        TaskType.CONFIRMATION_TEXTS: "#1abc9c",
        TaskType.CONTACT_FORM: "#2ecc71",
        TaskType.AFTERHOURS_FEE: "#34495e",
        TaskType.TIGHT_TURN: "#e67e22",
        TaskType.MANUAL: "#7f8c8d",
    }

    PRIORITY_COLORS = {
        Priority.CRITICAL: "#dc3545",
        Priority.HIGH: "#fd7e14",
        Priority.MEDIUM: "#ffc107",
        Priority.LOW: "#6c757d",
    }

    @property
    def type_icon(self):
        return self.TASK_TYPE_ICONS.get(self.task_type, "bi-question-circle")

    @property
    def type_color(self):
        return self.TASK_TYPE_COLORS.get(self.task_type, "#7f8c8d")

    @property
    def priority_color(self):
        return self.PRIORITY_COLORS.get(self.priority, "#6c757d")


class CommunicationAttempt(models.Model):
    """
    Tracks every outbound contact attempt tied to a task.
    Modeled after LeadActivity in ghl_integration but focused on
    communication channels and outcomes.
    """

    class Channel(models.TextChoices):
        PHONE_CALL = "call", "Phone Call"
        SMS = "sms", "SMS"
        EMAIL = "email", "Email"

    class Outcome(models.TextChoices):
        ANSWERED = "answered", "Answered"
        VOICEMAIL = "voicemail", "Voicemail"
        NO_ANSWER = "no_answer", "No Answer"
        BUSY = "busy", "Busy"
        SENT = "sent", "Sent"
        DELIVERED = "delivered", "Delivered"
        FAILED = "failed", "Failed"
        BOUNCED = "bounced", "Bounced"

    task = models.ForeignKey(
        OperationalTask, on_delete=models.CASCADE, related_name="comm_attempts"
    )
    channel = models.CharField(max_length=10, choices=Channel.choices)
    outcome = models.CharField(max_length=20, choices=Outcome.choices)
    staff_user = models.ForeignKey(
        "auth.User", on_delete=models.SET_NULL, null=True, related_name="comm_attempts"
    )
    contact_value = models.CharField(
        max_length=200, blank=True, help_text="Phone number or email used"
    )
    notes = models.TextField(blank=True)
    duration_seconds = models.PositiveIntegerField(
        null=True, blank=True, help_text="Call duration in seconds"
    )
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(
                fields=["task", "-created_at"], name="idx_comm_task_time"
            ),
        ]
        verbose_name = "Communication Attempt"
        verbose_name_plural = "Communication Attempts"

    def __str__(self):
        return f"{self.get_channel_display()} → {self.get_outcome_display()} (Task #{self.task_id})"


class StaffActivity(models.Model):
    """
    Passive tracking for owner visibility into staff behavior.
    Separate from AuditLog (which tracks model-level changes).
    This tracks operational behavior: page views, task actions, response times.
    """

    class ActionType(models.TextChoices):
        PAGE_VIEW = "page_view", "Page View"
        TASK_CLAIMED = "task_claimed", "Task Claimed"
        TASK_COMPLETED = "task_completed", "Task Completed"
        TASK_SNOOZED = "task_snoozed", "Task Snoozed"
        TASK_CREATED = "task_created", "Task Created"
        TASK_ASSIGNED = "task_assigned", "Task Assigned"
        COMM_LOGGED = "comm_logged", "Communication Logged"
        FLIGHT_MATCHED = "flight_matched", "Flight Time Matched"
        # ── Dispatch Shift System ──
        SHIFT_OPENED = "shift_opened", "Shift Checklist Opened"
        SHIFT_ROW_CONFIRMED = "shift_row_confirmed", "Checklist Row Confirmed"
        SHIFT_EXCEPTION_RAISED = "shift_exception", "Shift Exception Raised"
        SHIFT_COMPLETED = "shift_completed", "Shift Checklist Completed"
        SHIFT_REOPENED = "shift_reopened", "Shift Checklist Reopened"

    user = models.ForeignKey(
        "auth.User", on_delete=models.CASCADE, related_name="staff_activities"
    )
    action_type = models.CharField(max_length=30, choices=ActionType.choices)
    path = models.CharField(
        max_length=500, blank=True, help_text="URL path for page views"
    )
    task = models.ForeignKey(
        OperationalTask,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="staff_activities",
    )
    metadata = models.JSONField(default=dict, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(
                fields=["user", "-created_at"], name="idx_staff_user_time"
            ),
            models.Index(
                fields=["action_type", "-created_at"], name="idx_staff_action_time"
            ),
        ]
        verbose_name = "Staff Activity"
        verbose_name_plural = "Staff Activities"

    def __str__(self):
        return f"{self.user} — {self.get_action_type_display()} @ {self.created_at:%H:%M}"


class EmailLog(models.Model):
    """
    Tracks every email sent from the system for staff metrics and auditing.
    """

    class EmailType(models.TextChoices):
        CONFIRMATION = "confirmation", "Reservation Confirmation"
        PAYMENT_REMINDER = "payment_reminder", "Payment Reminder"
        DRIVER_STATEMENT = "driver_statement", "Driver Payment Statement"
        AGENT_COMMISSION = "agent_commission", "Agent Commission Statement"
        AGENCY_COMMISSION = "agency_commission", "Agency Commission Statement"
        LEAD_QUOTE = "lead_quote", "Lead Quote"
        ADMIN_REPORT = "admin_report", "Admin Commission Report"
        OTHER = "other", "Other"

    email_type = models.CharField(
        max_length=30, choices=EmailType.choices, default=EmailType.OTHER,
    )
    sent_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name="emails_sent",
        help_text="Staff member who triggered the send",
    )
    recipient_email = models.EmailField()
    subject = models.CharField(max_length=255, blank=True, default="")
    reservation = models.ForeignKey(
        "reservations.Reservation", on_delete=models.SET_NULL,
        null=True, blank=True, related_name="email_logs",
    )
    success = models.BooleanField(default=True)
    sent_at = models.DateTimeField(auto_now_add=True, db_index=True)
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-sent_at"]
        indexes = [
            models.Index(fields=["sent_by", "-sent_at"], name="idx_email_user_time"),
            models.Index(fields=["email_type", "-sent_at"], name="idx_email_type_time"),
        ]

    def __str__(self):
        return f"{self.get_email_type_display()} → {self.recipient_email} @ {self.sent_at:%Y-%m-%d %H:%M}"


class TimeClockShift(models.Model):
    """
    One clock-in → clock-out span for an office staff member (dispatcher).

    Open shift = ``clock_out_at IS NULL``. Breaks (``TimeClockBreak`` children)
    are ALWAYS unpaid, so net worked time = gross span − total break time.
    The state-machine logic that mutates these rows lives in ``ops/services.py``.
    """

    class State(models.TextChoices):
        CLOCKED_OUT = "clocked_out", "Clocked Out"
        CLOCKED_IN = "clocked_in", "Clocked In"
        ON_BREAK = "on_break", "On Break"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="timeclock_shifts",
    )
    clock_in_at = models.DateTimeField(db_index=True)
    clock_out_at = models.DateTimeField(
        null=True, blank=True, help_text="NULL while the shift is open."
    )
    note = models.TextField(blank=True)
    auto_closed = models.BooleanField(
        default=False,
        help_text="Closed automatically because it was left open too long.",
    )

    # ── Unscheduled clock-in approval ──
    # Set at clock-in time when the punch fell outside the resolved schedule
    # (day off, or outside the planned window). The shift still opens — staff
    # are never blocked from working — but it waits for an admin decision.
    # Blank approval_status = a normal, in-schedule punch (or admin-entered).
    class Approval(models.TextChoices):
        PENDING = "pending", "Pending approval"
        APPROVED = "approved", "Approved"
        DENIED = "denied", "Denied"

    unscheduled = models.BooleanField(
        default=False,
        help_text="Clock-in fell outside this staffer's planned schedule.",
    )
    approval_status = models.CharField(
        max_length=10, choices=Approval.choices, blank=True, default="",
        help_text="Only set for unscheduled clock-ins. Blank = no approval needed.",
    )
    approval_reason = models.CharField(
        max_length=200, blank=True, default="",
        help_text="Why the clock-in was flagged (shown to the admin).",
    )
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="decided_timeclock_shifts",
        help_text="Admin who approved or denied the unscheduled clock-in.",
    )
    approved_at = models.DateTimeField(null=True, blank=True)

    edited_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="edited_timeclock_shifts",
        help_text="Set when an admin corrects the times.",
    )
    edited_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-clock_in_at"]
        indexes = [
            models.Index(fields=["user", "-clock_in_at"], name="idx_tcshift_user_time"),
            # Partial index for the hot "who is currently clocked in" query.
            models.Index(
                fields=["clock_out_at"],
                name="idx_tcshift_open",
                condition=models.Q(clock_out_at__isnull=True),
            ),
            # Partial index for the manage page's approval queue.
            models.Index(
                fields=["approval_status"],
                name="idx_tcshift_pending",
                condition=models.Q(approval_status="pending"),
            ),
        ]
        constraints = [
            # DB-level guard: a user can have at most one open shift at a time.
            models.UniqueConstraint(
                fields=["user"],
                condition=models.Q(clock_out_at__isnull=True),
                name="uniq_open_shift_per_user",
            ),
        ]
        verbose_name = "Time Clock Shift"
        verbose_name_plural = "Time Clock Shifts"

    def __str__(self):
        return f"{self.user} — {self.clock_in_at:%m/%d %H:%M}"

    # ── State ──
    @property
    def is_open(self):
        return self.clock_out_at is None

    @property
    def needs_approval(self):
        return self.approval_status == self.Approval.PENDING

    @property
    def open_break(self):
        """The currently-open break, if any. Uses prefetched ``.breaks`` (no extra query)."""
        for b in self.breaks.all():
            if b.break_end_at is None:
                return b
        return None

    @property
    def state(self):
        if not self.is_open:
            return self.State.CLOCKED_OUT
        return self.State.ON_BREAK if self.open_break else self.State.CLOCKED_IN

    # ── Durations — every method accepts an explicit ``now`` so callers/tests can pin time. ──
    def break_seconds(self, now=None):
        now = now or timezone.now()
        total = 0.0
        for b in self.breaks.all():
            end = b.break_end_at or now
            total += max(0.0, (end - b.break_start_at).total_seconds())
        return total

    def gross_seconds(self, now=None):
        now = now or timezone.now()
        end = self.clock_out_at or now
        return max(0.0, (end - self.clock_in_at).total_seconds())

    def worked_seconds(self, now=None):
        now = now or timezone.now()
        return max(0.0, self.gross_seconds(now) - self.break_seconds(now))

    @property
    def gross_minutes(self):
        return int(self.gross_seconds() // 60)

    @property
    def break_minutes(self):
        return int(self.break_seconds() // 60)

    @property
    def worked_minutes(self):
        return int(self.worked_seconds() // 60)


class TimeClockRequest(models.Model):
    """
    A staffer's ask to clock in OUTSIDE their planned schedule. No shift exists
    and no time counts until the admin approves AND the staffer then actually
    clocks in — approval is a grant, not a punch, so a late approval never
    leaves a timer running for somebody who already went home.

    Lifecycle: pending -> approved (a grant, valid ~2h) -> used (consumed by
    the clock-in it authorised, linked via ``shift``); or denied / cancelled
    (staffer withdrew, or clocked in normally once their window arrived) /
    expired (nobody acted in time). One pending request per user, DB-enforced.
    """

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        APPROVED = "approved", "Approved — not yet used"
        DENIED = "denied", "Denied"
        USED = "used", "Used"
        CANCELLED = "cancelled", "Cancelled"
        EXPIRED = "expired", "Expired"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="timeclock_requests",
    )
    requested_at = models.DateTimeField(db_index=True)
    reason = models.CharField(
        max_length=200, blank=True, default="",
        help_text="Why the punch was outside the schedule (shown to the admin).",
    )
    status = models.CharField(
        max_length=10, choices=Status.choices, default=Status.PENDING,
    )
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="decided_timeclock_requests",
    )
    decided_at = models.DateTimeField(null=True, blank=True)
    decision_note = models.CharField(
        max_length=200, blank=True, default="",
        help_text="Shown to the staffer when a request is denied.",
    )
    shift = models.ForeignKey(
        TimeClockShift,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="approval_requests",
        help_text="The shift this grant opened, once used.",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-requested_at"]
        indexes = [
            models.Index(fields=["user", "-requested_at"], name="idx_tcreq_user_time"),
            models.Index(
                fields=["status"],
                name="idx_tcreq_pending",
                condition=models.Q(status="pending"),
            ),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["user"],
                condition=models.Q(status="pending"),
                name="uniq_pending_tcrequest_per_user",
            ),
        ]
        verbose_name = "Time Clock Request"
        verbose_name_plural = "Time Clock Requests"

    def __str__(self):
        return f"{self.user} — asked {self.requested_at:%m/%d %H:%M} [{self.status}]"


class TimeClockBreak(models.Model):
    """One unpaid break within a shift. Open break = ``break_end_at IS NULL``."""

    shift = models.ForeignKey(
        TimeClockShift,
        on_delete=models.CASCADE,
        related_name="breaks",
    )
    break_start_at = models.DateTimeField(db_index=True)
    break_end_at = models.DateTimeField(
        null=True, blank=True, help_text="NULL while the break is in progress."
    )
    auto_closed = models.BooleanField(
        default=False,
        help_text="Closed automatically because the shift was clocked out while on break.",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["break_start_at"]
        indexes = [
            models.Index(fields=["shift", "break_start_at"], name="idx_tcbreak_shift"),
        ]
        constraints = [
            # A shift can have at most one open break at a time.
            models.UniqueConstraint(
                fields=["shift"],
                condition=models.Q(break_end_at__isnull=True),
                name="uniq_open_break_per_shift",
            ),
        ]
        verbose_name = "Time Clock Break"
        verbose_name_plural = "Time Clock Breaks"

    def __str__(self):
        return f"Break {self.break_start_at:%m/%d %H:%M} (shift #{self.shift_id})"

    @property
    def is_open(self):
        return self.break_end_at is None

    @property
    def minutes(self):
        end = self.break_end_at or timezone.now()
        return int(max(0.0, (end - self.break_start_at).total_seconds()) // 60)


# ── Shift roles ───────────────────────────────────────────────────────
# An *assigned* duty for a shift, distinct from the board's derived
# "earliest in / latest out". Blank means unassigned — the board then falls
# back to deriving it from the hours, which is what it always did.
STAFF_ROLE_CHOICES = [
    ("opener", "Opener"),
    ("mid", "Mid-day"),
    ("closer", "Closer"),
    ("both", "Opener + Closer"),
]
STAFF_ROLE_LABELS = dict(STAFF_ROLE_CHOICES)

# ── Work location ─────────────────────────────────────────────────────
# WHERE a scheduled shift happens: in the office or from home. Blank means
# not tracked — a roster that never sets it reads exactly as before. The
# weekly row carries the recurring answer; an override's location flips a
# single date (the usual-WFH person coming in for a meeting) without
# touching the pattern.
WORK_LOCATION_CHOICES = [
    ("office", "In office"),
    ("remote", "Work from home"),
]
WORK_LOCATION_LABELS = dict(WORK_LOCATION_CHOICES)
# Compact labels for chips and badges.
WORK_LOCATION_SHORT = {"office": "Office", "remote": "WFH"}


class StaffWeeklySchedule(models.Model):
    """
    A dispatcher's planned recurring hours for one weekday (admin-set, view-only
    for staff). Mirrors drivers.DriverWeeklySchedule but uses TimeField for
    half-hour precision. Times are Eastern wall-clock. One row per (user, weekday).

    ``role`` is the *assigned* duty for that shift (opener/closer/…). It is
    deliberately separate from the hours: the earliest person in isn't always the
    one who owns opening. Blank = unassigned, and the board derives it from hours.
    """

    DAY_CHOICES = [
        (0, "Monday"),
        (1, "Tuesday"),
        (2, "Wednesday"),
        (3, "Thursday"),
        (4, "Friday"),
        (5, "Saturday"),
        (6, "Sunday"),
    ]

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="weekly_schedule_rows",
    )
    day_of_week = models.IntegerField(choices=DAY_CHOICES)
    is_working = models.BooleanField(default=True)
    start_time = models.TimeField(null=True, blank=True, help_text="Eastern wall-clock; null when off.")
    end_time = models.TimeField(null=True, blank=True, help_text="Eastern wall-clock; null when off.")
    role = models.CharField(
        max_length=12, choices=STAFF_ROLE_CHOICES, blank=True, default="",
        help_text="Assigned duty for this shift. Blank = derive from hours.",
    )
    location = models.CharField(
        max_length=10, choices=WORK_LOCATION_CHOICES, blank=True, default="",
        help_text="Where they work this weekday (office / WFH). Blank = not tracked.",
    )
    note = models.CharField(max_length=200, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ("user", "day_of_week")
        ordering = ["user", "day_of_week"]
        indexes = [
            models.Index(fields=["user", "day_of_week"], name="idx_staffwk_user_day"),
        ]
        verbose_name = "Staff Weekly Schedule"
        verbose_name_plural = "Staff Weekly Schedules"

    def __str__(self):
        day_name = dict(self.DAY_CHOICES).get(self.day_of_week, "?")
        if not self.is_working:
            return f"{self.user} — {day_name}: OFF"
        return f"{self.user} — {day_name}: {self.start_time:%H:%M}–{self.end_time:%H:%M}"


class StaffScheduleOverride(models.Model):
    """
    A one-off exception to a dispatcher's weekly schedule. Takes priority over the
    weekly row. Single day, or a range via end_date. Mirrors
    drivers.DriverDateOverride, approval workflow included.

    Two ways one gets created:

    * A manager adds it directly (schedule editor / staffing board) — those land
      ``status="approved"`` and take effect immediately, which is the historical
      behaviour and why "approved" is the default.
    * A dispatcher requests time off from their own schedule page — those land
      ``status="pending"`` with ``requested_by_staff=True`` and change *nothing*
      until a manager approves. Only approved rows are visible to the resolver.
    """

    KIND_CHOICES = [
        ("off", "Off"),
        ("custom_hours", "Custom hours"),
        ("note", "Note only"),
    ]
    # No "PTO" here on purpose — the company doesn't offer paid time off, so
    # the reasons mirror drivers.DriverDateOverride (which dropped PTO too).
    REASON_CHOICES = [
        ("vacation", "Vacation"),
        ("sick", "Sick"),
        ("personal", "Personal"),
        ("appointment", "Appointment"),
        ("unpaid", "Unpaid"),
        ("other", "Other"),
    ]
    STATUS_CHOICES = [
        ("approved", "Approved"),
        ("pending", "Pending review"),
        ("denied", "Denied"),
        ("cancelled", "Cancelled"),
    ]

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="schedule_overrides",
    )
    date = models.DateField(help_text="First day this override applies.")
    end_date = models.DateField(
        null=True, blank=True, help_text="Last day; blank = single day."
    )
    kind = models.CharField(max_length=16, choices=KIND_CHOICES, default="off")
    start_time = models.TimeField(null=True, blank=True, help_text="Eastern wall-clock; for custom_hours.")
    end_time = models.TimeField(null=True, blank=True, help_text="Eastern wall-clock; for custom_hours.")
    role = models.CharField(
        max_length=12, choices=STAFF_ROLE_CHOICES, blank=True, default="",
        help_text="One-off assigned duty for these dates. Blank = keep the recurring role.",
    )
    location = models.CharField(
        max_length=10, choices=WORK_LOCATION_CHOICES, blank=True, default="",
        help_text="One-off work location for these dates (e.g. in office on a usual WFH day). Blank = keep the weekly setting.",
    )
    reason = models.CharField(
        max_length=16, choices=REASON_CHOICES, blank=True, default="",
        help_text="Why the time off — only meaningful for 'off' entries.",
    )
    note = models.CharField(max_length=200, blank=True, default="")
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="created_staff_overrides",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    # Approval workflow. Manager-created rows default to "approved" so existing
    # data and admin behaviour are unchanged; staff-submitted requests land
    # "pending" and only affect the schedule once approved.
    status = models.CharField(
        max_length=12, choices=STATUS_CHOICES, default="approved",
        help_text="Only 'approved' rows change anyone's schedule.",
    )
    requested_by_staff = models.BooleanField(
        default=False,
        help_text="True when the dispatcher submitted this themselves.",
    )
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="decided_staff_overrides",
    )
    decided_at = models.DateTimeField(null=True, blank=True)
    denial_reason = models.CharField(
        max_length=200, blank=True, default="",
        help_text="Shown to the dispatcher when a request is declined.",
    )

    class Meta:
        ordering = ["date", "user"]
        indexes = [
            models.Index(fields=["user", "date"], name="idx_staffov_user_date"),
            models.Index(fields=["user", "date", "end_date"], name="idx_staffov_range"),
            models.Index(fields=["status", "date"], name="idx_staffov_status_date"),
        ]
        verbose_name = "Staff Schedule Override"
        verbose_name_plural = "Staff Schedule Overrides"

    def applies_on(self, target_date):
        if self.end_date is None:
            return self.date == target_date
        return self.date <= target_date <= self.end_date

    @property
    def date_range_display(self):
        if self.end_date is None or self.end_date == self.date:
            return self.date.strftime("%b %d, %Y")
        if self.date.year == self.end_date.year:
            return f"{self.date.strftime('%b %d')} – {self.end_date.strftime('%b %d, %Y')}"
        return f"{self.date.strftime('%b %d, %Y')} – {self.end_date.strftime('%b %d, %Y')}"

    @property
    def day_count(self):
        return ((self.end_date or self.date) - self.date).days + 1

    @property
    def reason_label(self):
        return dict(self.REASON_CHOICES).get(self.reason, "")

    @property
    def status_label(self):
        return dict(self.STATUS_CHOICES).get(self.status, self.status)

    @property
    def is_time_off(self):
        return self.kind == "off"

    def __str__(self):
        kind_label = dict(self.KIND_CHOICES).get(self.kind, self.kind)
        suffix = "" if self.status == "approved" else f" [{self.status}]"
        return f"{self.user} — {self.date_range_display}: {kind_label}{suffix}"


class StaffOnCall(models.Model):
    """
    A dispatcher marked on-call for one date's overnight window (default 12 AM–6 AM).

    On-call is *additive* — it does NOT replace the person's regular schedule; the
    same day they may also work a normal shift (so this is a separate row, not a
    StaffScheduleOverride, which the resolver treats as replacing the day). It is
    ad-hoc per date (no recurring rotation yet). Times are Eastern wall-clock and
    fall on ``date`` itself (00:00–06:00), so an on-call window does not cross
    midnight. On-call is *planned* coverage only — logging that someone actually
    took the on-call (paid, but not hourly) is a separate "actual" concept, kept
    apart from this the same way TimeClockShift is kept apart from the schedule.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="oncall_shifts",
    )
    date = models.DateField(db_index=True, help_text="The calendar date whose early hours (default 12 AM–6 AM) this covers.")
    start_time = models.TimeField(default=time(0, 0), help_text="Eastern wall-clock; on-call window start.")
    end_time = models.TimeField(default=time(6, 0), help_text="Eastern wall-clock; on-call window end (same day, no midnight cross).")
    note = models.CharField(max_length=200, blank=True, default="")
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="created_oncall_shifts",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ("user", "date")
        ordering = ["date", "user"]
        indexes = [
            models.Index(fields=["date"], name="idx_oncall_date"),
            models.Index(fields=["user", "date"], name="idx_oncall_user_date"),
        ]
        verbose_name = "Staff On-Call"
        verbose_name_plural = "Staff On-Call"

    def __str__(self):
        return f"{self.user} — {self.date:%b %d, %Y}: on-call {self.start_time:%H:%M}–{self.end_time:%H:%M}"


class StaffExtraShift(models.Model):
    """
    A *second* (or third) shift for one dispatcher on one day — the split-shift case.

    Why this is additive rather than another schedule row
    ----------------------------------------------------
    ``StaffWeeklySchedule`` is unique per (user, weekday) and
    ``resolve_staff_schedule`` returns exactly one start/end, so the primary
    schedule can only ever describe one continuous window. Rather than break that
    contract — the time clock, the dispatcher's own page and the board all read
    it — a split day is modelled as the primary window plus one or more extras:

        Iris, Wednesday:  9 AM – 1 PM  (primary, weekly schedule)
                          5 PM – 9 PM  (extra, this row)

    Same precedent as ``StaffOnCall``: additive, never replacing. The long gap in
    between is simply unscheduled — this is two shifts, not one shift with a
    break, so break tracking stays where it belongs (``TimeClockBreak``).

    Recurring or one-off, not both
    ------------------------------
    Set ``day_of_week`` for a shift that repeats every week, or ``date`` for a
    single day. Exactly one must be set; ``clean`` enforces it.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="extra_shifts",
    )
    day_of_week = models.IntegerField(
        null=True, blank=True, choices=StaffWeeklySchedule.DAY_CHOICES,
        help_text="For a weekly split shift. Leave blank for a one-off date.",
    )
    date = models.DateField(
        null=True, blank=True, db_index=True,
        help_text="For a one-off extra shift. Leave blank for a recurring weekday.",
    )
    start_time = models.TimeField(help_text="Eastern wall-clock.")
    end_time = models.TimeField(help_text="Eastern wall-clock; may cross midnight.")
    role = models.CharField(
        max_length=12, choices=STAFF_ROLE_CHOICES, blank=True, default="",
        help_text="Assigned duty for this shift (an evening half often closes).",
    )
    note = models.CharField(max_length=200, blank=True, default="")
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True, blank=True, on_delete=models.SET_NULL,
        related_name="created_staff_extra_shifts",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["user", "day_of_week", "date", "start_time"]
        indexes = [
            models.Index(fields=["user", "day_of_week"], name="idx_staffextra_user_dow"),
            models.Index(fields=["user", "date"], name="idx_staffextra_user_date"),
        ]
        verbose_name = "Staff Extra Shift"
        verbose_name_plural = "Staff Extra Shifts"

    def clean(self):
        from django.core.exceptions import ValidationError
        if (self.day_of_week is None) == (self.date is None):
            raise ValidationError("Set either a weekday (recurring) or a date (one-off), not both.")
        if self.start_time == self.end_time:
            raise ValidationError("Start and end times can't match.")

    @property
    def is_recurring(self):
        return self.day_of_week is not None

    def applies_on(self, target_date):
        if self.date is not None:
            return self.date == target_date
        return self.day_of_week == target_date.weekday()

    @property
    def when_display(self):
        if self.is_recurring:
            return f"every {dict(StaffWeeklySchedule.DAY_CHOICES).get(self.day_of_week, '?')}"
        return self.date.strftime("%b %d, %Y")

    def __str__(self):
        return (f"{self.user} — {self.when_display}: extra "
                f"{self.start_time:%H:%M}–{self.end_time:%H:%M}")


# ══════════════════════════════════════════════════════════════════════
# Dispatch Shift System — Open / Close checklists.
#
# The operating layer AROUND the dispatch system: who opened the shift, what
# the four automated checks said at the time, which human-verified queues were
# confirmed, what was left outstanding and who owns it.
#
# THE ONE RULE (docs/dispatch-ops/SHIFT-SYSTEM-AUDIT.md §13): this layer is a
# READER. It never writes a Leg, a leg status, a vehicle plan, a draft or a
# scheduler dial. The system rows are COMPUTED at read time from the board and
# the task queue; `counts_snapshot` below is history, never the live answer.
# ══════════════════════════════════════════════════════════════════════


# Row keys. `system` rows are computed and cannot be ticked; `human` rows are
# tap-to-confirm because the app cannot see those queues at all (audit §7).
CHECK_UNASSIGNED = "unassigned"
CHECK_UNCONFIRMED = "unconfirmed"
CHECK_FLIGHT = "flight"
CHECK_CONFLICTS = "conflicts"
CHECK_MOVES = "moves"

SYSTEM_ROW_LABELS = {
    CHECK_UNASSIGNED: "Unassigned trips",
    CHECK_UNCONFIRMED: "Chauffeurs who have not confirmed",
    CHECK_FLIGHT: "Flight alerts not reviewed",
    CHECK_CONFLICTS: "Conflict / tight-turn tasks open",
    CHECK_MOVES: "Turns the new pickup times broke",
}

# These five are on a person's word because the app genuinely cannot see them:
# there is no RingCentral integration, no WhatsApp integration, nothing reads
# the mailbox, and the GoHighLevel inbox is not readable from here either
# (audit §7). A dispatcher reasonably assumes the app can check anything it can
# SEND through, so each row says in plain words what is being confirmed rather
# than leaving them to guess.
# The WhatsApp row is NOT the same job at both ends of the day, which is why
# open and close carry different keys rather than sharing one:
#   * opening  — you announce you're on, then READ the overnight backlog;
#   * closing  — you WRITE what the next shift is walking into.
# Sharing a key put "Opening message sent" on the close checklist.
HUMAN_ROW_LABELS = {
    # ── Open ──
    # RingCentral comes first: if nobody is signed in, calls are being missed
    # right now, which outranks whether a queue is tidy.
    "phone": "RingCentral logged in, ringer on",
    # WhatsApp is the team's channel (audit §7.7) — the same place the close
    # summary gets pasted. The row named "texts" before, which sent openers to
    # the wrong app.
    "triage": "Opening message sent on WhatsApp, chat skimmed",
    # ── Close ──
    # No "ringer on" at the close: the opener set that hours ago, and asking
    # again teaches people to tick past a row they already did. What matters
    # now is that the line is still up as you hand it over.
    "phone_close": "RingCentral logged in and working",
    "handover": "Closing message sent on WhatsApp, with any notes",
    # ── Both ──
    "sms": "RingCentral texts at zero",
    "email": "Email at zero",
    "ghl": "GoHighLevel at zero",
}

HUMAN_ROW_HINTS = {
    "phone": "Signed in to RingCentral on this machine, and you can hear it ring.",
    "triage": (
        "Post that you're opening in the WhatsApp group, then scan back over "
        "anything that came in overnight \u2014 changes, cancellations, notes "
        "someone left you. A quick scan, not a reply: the inboxes are the last "
        "step."
    ),
    "phone_close": "Still signed in and taking calls as you hand the line over.",
    "handover": (
        "The last thing you do. Finishing the close writes the message for "
        "you \u2014 hit Copy, paste it into the WhatsApp group, and add anything "
        "the next shift needs to know."
    ),
    "sms": "Nobody left waiting on a reply.",
    "email": "Nothing in the inbox still needs answering.",
    "ghl": "No conversation still waiting on us.",
}

# Founder direction 2026-09-12: the open-task count is a CLOSE check, not an
# OPEN one. Measured at ~71 conflict/tight-turn tasks a day with ~66% of closes
# buying nothing (docs/scheduling-redesign/06_DAY_MANAGER.md §0.2), it is noise
# at a 7:15 AM gate. Conflicts still show red on the board the opener works.
# The Opener SOP's own running order: unassigned (step 1), confirmations
# (step 2), flights (step 3), then the conflicts the new pickup times created
# (step 4). Conflicts are on the open BECAUSE the SOP gates "today is protected"
# behind them — a flight that moves 40 minutes makes a turn that was fine at
# 6:30 impossible by 7:00, and that is exactly what step 4 sweeps for.
DEFAULT_OPEN_SYSTEM_ROWS = [CHECK_UNASSIGNED, CHECK_UNCONFIRMED, CHECK_FLIGHT,
                            CHECK_MOVES]
DEFAULT_CLOSE_SYSTEM_ROWS = [CHECK_UNASSIGNED, CHECK_UNCONFIRMED, CHECK_FLIGHT,
                             CHECK_CONFLICTS]
# OPEN: systems up and the WhatsApp opening post come first (SOP "opener on"
# and step 0); the three inboxes come last, in the SOP's strict order — texts,
# then email, then GoHighLevel, one at a time.
DEFAULT_OPEN_HUMAN_ROWS = ["phone", "triage", "sms", "email", "ghl"]
# CLOSE: the same inboxes, but the WhatsApp post moves to the END. At the open
# it is the FIRST thing because it tells you what you are walking into; at the
# close it is the LAST thing because it reports what you are handing over, and
# it cannot be written until everything above it is settled.
DEFAULT_CLOSE_HUMAN_ROWS = ["phone_close", "sms", "email", "ghl", "handover"]

# Rows that were once on the list. A checklist created before a row was retired
# still carries it, and a raw key like "missed_calls" rendering on the floor is
# worse than the row itself ever was. (Missed calls folded into the phone row —
# it read as a second RingCentral line.)
RETIRED_ROW_LABELS = {
    "missed_calls": "Missed calls returned",
}


class ShiftSettings(models.Model):
    """Singleton (pk=1) for the shift layer's operator-editable numbers.

    Mirrors the established pattern of ``dispatching.SchedulerSettings`` —
    one row, defaults in the model, edited from a page rather than code — with
    one deliberate difference: **no module-global cache.** SchedulerSettings
    memoises itself per process, so under three gunicorn workers a save reaches
    only the worker that served it until restart. This row is small and indexed;
    read it per request.
    """

    board_safe_target = models.TimeField(
        default=time(7, 15),
        help_text="Eastern wall-clock backstop. The board is safe by this time however late the open started.",
    )
    open_complete_target = models.TimeField(
        default=time(8, 0),
        help_text="Eastern wall-clock backstop. Opening is fully done by this time.",
    )
    board_safe_minutes = models.PositiveSmallIntegerField(
        default=45,
        help_text="Opener SOP: minutes from starting the open to a safe board. 0 = wall-clock only.",
    )
    open_complete_minutes = models.PositiveSmallIntegerField(
        default=90,
        help_text="Opener SOP: minutes from starting the open to a finished open. 0 = wall-clock only.",
    )
    close_target = models.TimeField(
        null=True, blank=True, default=time(21, 0),
        help_text="Eastern wall-clock target for finishing the close. Blank = untimed.",
    )

    open_system_rows = models.JSONField(
        default=list, blank=True,
        help_text="System row keys shown on Open Shift. Blank = the built-in default.",
    )
    close_system_rows = models.JSONField(
        default=list, blank=True,
        help_text="System row keys shown on Close Shift. Blank = the built-in default.",
    )
    open_human_rows = models.JSONField(
        default=list, blank=True,
        help_text="Human-verified row keys on Open Shift. Blank = the built-in default.",
    )
    close_human_rows = models.JSONField(
        default=list, blank=True,
        help_text="Human-verified row keys on Close Shift. Blank = the built-in default.",
    )

    summary_intro = models.CharField(
        max_length=200, blank=True, default="",
        help_text="Optional first line prepended to the pasteable completion message.",
    )

    class Meta:
        verbose_name = "Shift Settings"
        verbose_name_plural = "Shift Settings"

    def __str__(self):
        return "Shift Settings"

    @classmethod
    def load(cls):
        """The singleton row, created with defaults on first access."""
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    def rows_for(self, kind):
        """(system_row_keys, human_row_keys) for 'open' or 'close'.

        Human rows are per-kind for the same reason the system rows are: the
        two shifts ask different questions. Sharing one list is what put
        "Opening message sent" on the close checklist.
        """
        if kind == ShiftChecklist.Kind.OPEN:
            system = self.open_system_rows or DEFAULT_OPEN_SYSTEM_ROWS
            human = self.open_human_rows or DEFAULT_OPEN_HUMAN_ROWS
        else:
            system = self.close_system_rows or DEFAULT_CLOSE_SYSTEM_ROWS
            human = self.close_human_rows or DEFAULT_CLOSE_HUMAN_ROWS
        return list(system), list(human)

    def targets_for(self, kind):
        """The gate times in force, frozen onto a checklist when it opens."""
        if kind == ShiftChecklist.Kind.OPEN:
            return {
                "board_safe": self.board_safe_target.strftime("%H:%M"),
                "open_complete": self.open_complete_target.strftime("%H:%M"),
                "board_safe_min": self.board_safe_minutes,
                "open_complete_min": self.open_complete_minutes,
            }
        return {"close": self.close_target.strftime("%H:%M") if self.close_target else ""}


class ShiftChecklist(models.Model):
    """One Open or Close checklist for one service date.

    Open looks at TODAY, Close looks at TOMORROW — ``target_date`` below is the
    date the system rows are counted against, which is not the same as ``date``
    (the shift's own calendar day) for a close.
    """

    class Kind(models.TextChoices):
        OPEN = "open", "Open Shift"
        CLOSE = "close", "Close Shift"

    date = models.DateField(db_index=True, help_text="The shift's own calendar day (Eastern).")
    kind = models.CharField(max_length=5, choices=Kind.choices)

    opened_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="shift_checklists_opened",
    )
    opened_at = models.DateTimeField(null=True, blank=True)

    board_safe_at = models.DateTimeField(
        null=True, blank=True,
        help_text="Stamped when every system row is clear or documented and carried notes are owned.",
    )
    completed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="shift_checklists_completed",
    )
    completed_at = models.DateTimeField(null=True, blank=True)

    reopened_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="shift_checklists_reopened",
    )
    reopened_at = models.DateTimeField(null=True, blank=True)
    reopen_count = models.PositiveSmallIntegerField(default=0)

    #: The gate times in force when this checklist opened. Frozen so a later
    #: settings change can never turn a hit into a miss retroactively — the same
    #: honesty DayPlan.bookings_as_of keeps.
    targets = models.JSONField(default=dict, blank=True)
    #: The system counts at completion. HISTORY ONLY — never read as the live
    #: answer (audit §13.4).
    counts_snapshot = models.JSONField(default=dict, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-date", "kind"]
        constraints = [
            models.UniqueConstraint(fields=["date", "kind"], name="uniq_shift_checklist_per_day"),
        ]
        indexes = [
            models.Index(fields=["-date", "kind"], name="idx_shiftlist_date_kind"),
        ]
        permissions = [
            ("review_checklists", "Can review shift checklist history"),
            ("reopen_checklist", "Can reopen a completed shift checklist"),
            ("edit_exception_owner", "Can change who owns a shift exception"),
        ]
        verbose_name = "Shift Checklist"
        verbose_name_plural = "Shift Checklists"

    def __str__(self):
        return f"{self.get_kind_display()} {self.date:%Y-%m-%d}"

    # ── State ──
    @property
    def is_complete(self):
        return self.completed_at is not None

    @property
    def target_date(self):
        """The service date the system rows count against."""
        from datetime import timedelta
        return self.date + timedelta(days=1) if self.kind == self.Kind.CLOSE else self.date

    def _target_dt(self, key):
        """The gate time in force: whichever of the two limits lands first.

        The SOP sets an allowance measured from the moment the open starts
        (45 minutes to a safe board, 90 to a finished open). The wall-clock
        target is the backstop underneath it, because a 7 AM pickup does not
        care what time the opener sat down. Start an hour early and the
        allowance binds; start late and the wall clock does.
        """
        from datetime import datetime, timedelta
        targets = self.targets or {}

        wall = None
        raw = targets.get(key)
        if raw:
            try:
                hh, mm = (int(part) for part in raw.split(":"))
                wall = timezone.make_aware(
                    datetime.combine(self.date, time(hh, mm)),
                    timezone.get_current_timezone(),
                )
            except (ValueError, AttributeError, TypeError):
                wall = None

        elapsed = None
        minutes = targets.get(f"{key}_min")
        start = self._allowance_start()
        if minutes and start:
            try:
                elapsed = start + timedelta(minutes=int(minutes))
            except (ValueError, TypeError):
                elapsed = None

        if wall and elapsed:
            return min(wall, elapsed)
        return wall or elapsed

    def _allowance_start(self):
        """When the SOP's clock starts, or None if it does not apply.

        The allowance is "45 minutes from sitting down", which only means
        something when the checklist is being worked on its own day. Opened
        ahead of time — a close pointed at tomorrow, a day backfilled — it would
        anchor to the wrong date and judge a gate against a time on another
        day entirely. Then only the wall clock applies.
        """
        start = self.opened_at or self.created_at
        if start is None:
            return None
        if timezone.localtime(start).date() != self.date:
            return None
        return start

    def target_basis(self, key):
        """'allowance' | 'clock' | '' — which limit is setting the gate."""
        from datetime import datetime, timedelta
        targets = self.targets or {}
        minutes = targets.get(f"{key}_min")
        start = self._allowance_start()
        if not (minutes and start):
            return "clock" if targets.get(key) else ""
        effective = self._target_dt(key)
        if effective is None:
            return ""
        return "allowance" if effective == start + timedelta(minutes=int(minutes)) else "clock"

    def gate_status(self, key, stamped_at):
        """'on_time' | 'late' | 'pending' for a frozen target against a stamp."""
        target = self._target_dt(key)
        if stamped_at is None:
            return "pending"
        if target is None:
            return "on_time"
        return "on_time" if stamped_at <= target else "late"

    @property
    def board_safe_status(self):
        return self.gate_status("board_safe", self.board_safe_at)

    @property
    def complete_status(self):
        key = "open_complete" if self.kind == self.Kind.OPEN else "close"
        return self.gate_status(key, self.completed_at)


class ShiftChecklistRow(models.Model):
    """One check on one checklist.

    A ``system`` row's state is DERIVED from a live count and is persisted only
    so a completed checklist keeps its history. It can never be confirmed by
    hand — the view rejects that, because a row a dispatcher can tick is a row
    that stops meaning anything.
    """

    class Kind(models.TextChoices):
        SYSTEM = "system", "System-verified"
        HUMAN = "human", "Human-verified"

    class State(models.TextChoices):
        OPEN = "open", "Outstanding"
        CLEAR = "clear", "Clear"
        DOCUMENTED = "documented", "Documented exception"

    checklist = models.ForeignKey(
        ShiftChecklist, on_delete=models.CASCADE, related_name="rows",
    )
    key = models.CharField(max_length=24)
    kind = models.CharField(max_length=6, choices=Kind.choices)
    state = models.CharField(max_length=12, choices=State.choices, default=State.OPEN)
    position = models.PositiveSmallIntegerField(default=0)

    confirmed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="shift_rows_confirmed",
    )
    confirmed_at = models.DateTimeField(null=True, blank=True)
    note = models.CharField(max_length=200, blank=True, default="")

    #: Last computed count for a system row, refreshed on every page render.
    #: Display convenience only — the live count is always recomputed.
    last_count = models.IntegerField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["position", "id"]
        constraints = [
            models.UniqueConstraint(fields=["checklist", "key"], name="uniq_shift_row_per_checklist"),
        ]
        verbose_name = "Shift Checklist Row"
        verbose_name_plural = "Shift Checklist Rows"

    def __str__(self):
        return f"{self.checklist} · {self.key} ({self.state})"

    @property
    def label(self):
        return (
            SYSTEM_ROW_LABELS.get(self.key)
            or HUMAN_ROW_LABELS.get(self.key)
            or RETIRED_ROW_LABELS.get(self.key)
            or self.key.replace("_", " ").capitalize()
        )

    @property
    def is_system(self):
        return self.kind == self.Kind.SYSTEM


class ShiftException(models.Model):
    """Something outstanding at completion, with an owner and a next action.

    NO SILENT EXCEPTIONS: a checklist cannot complete while a row is neither
    clear nor covered by one of these.

    It REFERENCES existing objects rather than restating them — a conflict lives
    in ``OperationalTask``, a watch item in ``LegKeoi``, a trip in ``Leg``.
    Copying their text would create a second version that drifts (audit §13.4).
    """

    checklist = models.ForeignKey(
        ShiftChecklist, on_delete=models.CASCADE, related_name="exceptions",
    )
    row = models.ForeignKey(
        ShiftChecklistRow, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="exceptions",
    )

    what = models.TextField(help_text="What is outstanding.")
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
        related_name="shift_exceptions_owned",
        help_text="The person who owns it. Never blank — that is the whole point.",
    )
    next_action = models.CharField(max_length=200, help_text="What happens next.")
    next_action_at = models.DateTimeField(
        null=True, blank=True, help_text="When the next action is due, if it has a time.",
    )

    # ── References, never copies ──
    task = models.ForeignKey(
        "ops.OperationalTask", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="shift_exceptions",
    )
    leg = models.ForeignKey(
        "reservations.Leg", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="shift_exceptions",
    )
    keoi = models.ForeignKey(
        "reservations.LegKeoi", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="shift_exceptions",
    )

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="shift_exceptions_created",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    resolved_at = models.DateTimeField(null=True, blank=True)
    resolved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="shift_exceptions_resolved",
    )
    resolution_note = models.CharField(max_length=200, blank=True, default="")

    # ── Carry-forward ──
    carried_from = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="carried_to",
        help_text="The exception on the previous shift this one continues.",
    )
    acknowledged_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="shift_exceptions_acknowledged",
    )
    acknowledged_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["checklist"], name="idx_shiftexc_checklist"),
            models.Index(
                fields=["resolved_at"],
                name="idx_shiftexc_open",
                condition=models.Q(resolved_at__isnull=True),
            ),
        ]
        verbose_name = "Shift Exception"
        verbose_name_plural = "Shift Exceptions"

    def __str__(self):
        state = "open" if self.resolved_at is None else "resolved"
        return f"{self.what[:50]} — {self.owner} ({state})"

    @property
    def is_open(self):
        return self.resolved_at is None

    @property
    def needs_acknowledgement(self):
        """A carried-forward note nobody has taken ownership of yet."""
        return self.carried_from_id is not None and self.acknowledged_at is None

    @property
    def carry_depth(self):
        """How many shifts this has been carried across. 0 = raised here."""
        depth, node, guard = 0, self, 0
        while node.carried_from_id and guard < 20:
            depth += 1
            node = node.carried_from
            guard += 1
        return depth
