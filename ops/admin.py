from django.contrib import admin
from .models import OperationalTask, CommunicationAttempt, StaffActivity


class CommunicationAttemptInline(admin.TabularInline):
    model = CommunicationAttempt
    extra = 0
    readonly_fields = ("created_at",)


@admin.register(OperationalTask)
class OperationalTaskAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "task_type",
        "priority",
        "status",
        "title",
        "assigned_to",
        "due_at",
        "attempts",
        "created_at",
    )
    list_filter = ("task_type", "status", "priority", "assigned_to")
    search_fields = ("title", "description")
    readonly_fields = ("created_at", "updated_at")
    raw_id_fields = ("reservation", "leg", "lead", "assigned_to", "created_by", "resolved_by", "blocked_by")
    inlines = [CommunicationAttemptInline]
    date_hierarchy = "created_at"


@admin.register(CommunicationAttempt)
class CommunicationAttemptAdmin(admin.ModelAdmin):
    list_display = ("id", "task", "channel", "outcome", "staff_user", "created_at")
    list_filter = ("channel", "outcome")
    readonly_fields = ("created_at",)
    raw_id_fields = ("task", "staff_user")


@admin.register(StaffActivity)
class StaffActivityAdmin(admin.ModelAdmin):
    list_display = ("id", "user", "action_type", "path", "created_at")
    list_filter = ("action_type", "user")
    readonly_fields = ("created_at",)
    raw_id_fields = ("user", "task")
    date_hierarchy = "created_at"
