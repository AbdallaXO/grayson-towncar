"""
Operational task queue models for Grayson Towncar staff productivity layer.

Three models:
- OperationalTask: unified work-queue item for all operational task types
- CommunicationAttempt: tracks every outbound contact attempt tied to a task
- StaffActivity: passive tracking for owner visibility into staff behavior
"""

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


class StaffWeeklySchedule(models.Model):
    """
    A dispatcher's planned recurring hours for one weekday (admin-set, view-only
    for staff). Mirrors drivers.DriverWeeklySchedule but uses TimeField for
    half-hour precision. Times are Eastern wall-clock. One row per (user, weekday).
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
    A one-off exception to a dispatcher's weekly schedule (admin-set). Takes
    priority over the weekly row. Single day, or a range via end_date.
    Mirrors drivers.DriverDateOverride minus the approval workflow.
    """

    KIND_CHOICES = [
        ("off", "Off"),
        ("custom_hours", "Custom hours"),
        ("note", "Note only"),
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

    class Meta:
        ordering = ["date", "user"]
        indexes = [
            models.Index(fields=["user", "date"], name="idx_staffov_user_date"),
            models.Index(fields=["user", "date", "end_date"], name="idx_staffov_range"),
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

    def __str__(self):
        kind_label = dict(self.KIND_CHOICES).get(self.kind, self.kind)
        return f"{self.user} — {self.date_range_display}: {kind_label}"
