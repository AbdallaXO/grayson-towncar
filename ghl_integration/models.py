"""
Models for the GoHighLevel integration follow-up engine.

Includes:
- FollowUpSequence: Message templates per step/segment
- FollowUpTask: Scheduled outbound message work queue
- LeadActivity: Audit log for lead lifecycle events
- GHLSyncLog: Dead letter queue for failed GHL sync operations
"""

from django.db import models
from django.utils import timezone


class FollowUpSequence(models.Model):
    """
    Defines the message template for a given step and segment.
    Editable via Django admin — no deploy needed to change messaging.
    """

    SEGMENT_CHOICES = [
        ("general", "General"),
        ("airport_transfer", "Airport Transfer"),
        ("cruise_transfer", "Cruise Transfer"),
        ("theme_park", "Theme Park"),
        ("large_group", "Large Group"),
        ("repeat_customer", "Repeat Customer"),
        ("abandoned_quote", "Abandoned Quote"),
    ]

    step_number = models.PositiveSmallIntegerField(
        help_text="Step in the sequence (1-5)"
    )
    segment = models.CharField(
        max_length=30, choices=SEGMENT_CHOICES, default="general"
    )
    delay_hours = models.PositiveIntegerField(
        help_text="Hours after Step 1 send for this step (0=immediate, 4, 20, 48, 96)"
    )
    message_template = models.TextField(
        help_text="SMS body with {first_name}, {pickup_location}, {dropoff_location}, {pickup_date}, {estimated_price}, {vehicle_name} placeholders"
    )
    is_active = models.BooleanField(default=True)

    class Meta:
        unique_together = [("step_number", "segment")]
        ordering = ["segment", "step_number"]
        verbose_name = "Follow-Up Sequence Template"
        verbose_name_plural = "Follow-Up Sequence Templates"

    def __str__(self):
        return f"Step {self.step_number} — {self.get_segment_display()}"


class FollowUpTask(models.Model):
    """
    Work queue for scheduled outbound follow-up messages.
    One row per pending or completed message for a lead.
    """

    class StatusChoices(models.TextChoices):
        PENDING = "pending", "Pending"
        SENT = "sent", "Sent"
        CANCELLED = "cancelled", "Cancelled"
        FAILED = "failed", "Failed"
        SKIPPED = "skipped", "Skipped"

    lead = models.ForeignKey(
        "reservations.Lead", on_delete=models.CASCADE, related_name="follow_up_tasks"
    )
    step_number = models.PositiveSmallIntegerField()
    segment = models.CharField(max_length=30, blank=True)
    status = models.CharField(
        max_length=20, choices=StatusChoices.choices, default=StatusChoices.PENDING
    )
    scheduled_at = models.DateTimeField(
        help_text="When to send (already adjusted for send window)"
    )
    sent_at = models.DateTimeField(null=True, blank=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)
    cancel_reason = models.CharField(
        max_length=50, blank=True,
        help_text="replied, converted, expired_date"
    )
    message_body = models.TextField(
        blank=True, help_text="Rendered message saved after send for audit"
    )
    attempts = models.PositiveSmallIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [("lead", "step_number")]
        indexes = [
            models.Index(fields=["status", "scheduled_at"], name="idx_task_status_sched"),
        ]
        ordering = ["scheduled_at"]
        verbose_name = "Follow-Up Task"
        verbose_name_plural = "Follow-Up Tasks"

    def __str__(self):
        return f"Step {self.step_number} for Lead #{self.lead_id} — {self.status}"


class LeadActivity(models.Model):
    """Audit log for lead lifecycle events."""

    class ActivityType(models.TextChoices):
        SMS_SENT = "sms_sent", "SMS Sent"
        SMS_FAILED = "sms_failed", "SMS Failed"
        REPLY_RECEIVED = "reply_received", "Reply Received"
        CONVERTED = "converted", "Converted"
        STATUS_CHANGE = "status_change", "Status Change"
        SEQUENCE_STARTED = "sequence_started", "Sequence Started"
        SEQUENCE_STOPPED = "sequence_stopped", "Sequence Stopped"
        SEQUENCE_COMPLETED = "sequence_completed", "Sequence Completed"

    lead = models.ForeignKey(
        "reservations.Lead", on_delete=models.CASCADE, related_name="activities"
    )
    activity_type = models.CharField(max_length=30, choices=ActivityType.choices)
    description = models.TextField()
    metadata = models.JSONField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Lead Activity"
        verbose_name_plural = "Lead Activities"

    def __str__(self):
        return f"{self.get_activity_type_display()} — Lead #{self.lead_id}"


class GHLSyncLog(models.Model):
    """Dead letter queue for failed GHL sync operations with retry logic."""

    class ActionChoices(models.TextChoices):
        CREATE_CONTACT = "create_contact", "Create Contact"
        SEND_SMS = "send_sms", "Send SMS"
        UPDATE_STATUS = "update_status", "Update Status"
        ADD_TAG = "add_tag", "Add Tag"
        REMOVE_TAG = "remove_tag", "Remove Tag"

    class StatusChoices(models.TextChoices):
        PENDING = "pending", "Pending"
        SUCCESS = "success", "Success"
        FAILED = "failed", "Failed"
        DEAD_LETTER = "dead_letter", "Dead Letter"

    lead = models.ForeignKey(
        "reservations.Lead", on_delete=models.CASCADE, related_name="sync_logs"
    )
    action = models.CharField(max_length=30, choices=ActionChoices.choices)
    status = models.CharField(
        max_length=20, choices=StatusChoices.choices, default=StatusChoices.PENDING
    )
    attempts = models.PositiveIntegerField(default=0)
    max_attempts = models.PositiveIntegerField(default=5)
    last_attempt_at = models.DateTimeField(null=True, blank=True)
    next_retry_at = models.DateTimeField(null=True, blank=True)
    error_message = models.TextField(blank=True)
    request_payload = models.JSONField(null=True, blank=True)
    response_payload = models.JSONField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "GHL Sync Log"
        verbose_name_plural = "GHL Sync Logs"

    def __str__(self):
        return f"{self.get_action_display()} — {self.status} — Lead #{self.lead_id}"
