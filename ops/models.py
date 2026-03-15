"""
Operational task queue models for Grayson Towncar staff productivity layer.

Three models:
- OperationalTask: unified work-queue item for all operational task types
- CommunicationAttempt: tracks every outbound contact attempt tied to a task
- StaffActivity: passive tracking for owner visibility into staff behavior
"""

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
        CONTACT_FORM = "contact_form", "Contact Us"
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
        TaskType.CONTACT_FORM: "bi-envelope-paper",
        TaskType.MANUAL: "bi-pencil-square",
    }

    TASK_TYPE_COLORS = {
        TaskType.PAYMENT_CHASE: "#f39c12",
        TaskType.FLIGHT_VERIFICATION: "#3498db",
        TaskType.DRIVER_CONFLICT: "#e74c3c",
        TaskType.DRIVER_ASSIGNMENT: "#9b59b6",
        TaskType.CONTACT_FORM: "#2ecc71",
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
