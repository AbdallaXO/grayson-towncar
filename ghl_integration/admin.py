from django.contrib import admin
from .models import FollowUpSequence, FollowUpTask, LeadActivity, GHLSyncLog


@admin.register(FollowUpSequence)
class FollowUpSequenceAdmin(admin.ModelAdmin):
    list_display = ("step_number", "segment", "delay_hours", "is_active", "template_preview")
    list_filter = ("segment", "is_active", "step_number")
    list_editable = ("is_active",)
    ordering = ("segment", "step_number")

    def template_preview(self, obj):
        return obj.message_template[:80] + "..." if len(obj.message_template) > 80 else obj.message_template
    template_preview.short_description = "Template Preview"


@admin.register(FollowUpTask)
class FollowUpTaskAdmin(admin.ModelAdmin):
    list_display = ("lead", "step_number", "segment", "status", "scheduled_at", "sent_at", "attempts", "cancel_reason")
    list_filter = ("status", "step_number", "segment")
    search_fields = ("lead__first_name", "lead__last_name", "lead__email", "lead__phone")
    readonly_fields = ("message_body", "created_at")
    date_hierarchy = "scheduled_at"
    ordering = ("-scheduled_at",)
    raw_id_fields = ("lead",)

    actions = ["cancel_selected_tasks"]

    def cancel_selected_tasks(self, request, queryset):
        from django.utils import timezone
        count = queryset.filter(status="pending").update(
            status="cancelled",
            cancelled_at=timezone.now(),
            cancel_reason="manual",
        )
        self.message_user(request, f"Cancelled {count} pending task(s).")
    cancel_selected_tasks.short_description = "Cancel selected pending tasks"


@admin.register(LeadActivity)
class LeadActivityAdmin(admin.ModelAdmin):
    list_display = ("lead", "activity_type", "description_preview", "created_at")
    list_filter = ("activity_type", "created_at")
    search_fields = ("lead__first_name", "lead__last_name", "lead__email", "description")
    readonly_fields = ("lead", "activity_type", "description", "metadata", "created_at")
    date_hierarchy = "created_at"
    ordering = ("-created_at",)

    def description_preview(self, obj):
        return obj.description[:100] + "..." if len(obj.description) > 100 else obj.description
    description_preview.short_description = "Description"

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(GHLSyncLog)
class GHLSyncLogAdmin(admin.ModelAdmin):
    list_display = ("lead", "action", "status", "attempts", "last_attempt_at", "error_preview", "created_at")
    list_filter = ("status", "action", "created_at")
    search_fields = ("lead__first_name", "lead__last_name", "lead__email", "error_message")
    readonly_fields = ("request_payload", "response_payload", "created_at")
    date_hierarchy = "created_at"
    ordering = ("-created_at",)
    raw_id_fields = ("lead",)

    def error_preview(self, obj):
        if obj.error_message:
            return obj.error_message[:60] + "..." if len(obj.error_message) > 60 else obj.error_message
        return "-"
    error_preview.short_description = "Error"
