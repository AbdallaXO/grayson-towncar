from django.contrib import admin
from django.urls import reverse
from django.utils.html import format_html
from .models import Payment


@admin.register(Payment)
class UserPaymentAdmin(admin.ModelAdmin):
    list_display = (
        "status_badge",
        "payment_type",
        "customer",
        "reservation_link",
        "amount_display",
        "created_at_display",
    )
    list_display_links = ("status_badge", "customer", "reservation_link")
    list_filter = ("payment_type", "status", "created_at")
    date_hierarchy = "created_at"
    ordering = ("-created_at",)
    search_fields = (
        "customer__first_name",
        "customer__last_name",
        "reservation__id",
        "stripe_payment_intent_id",
    )
    autocomplete_fields = ("customer", "reservation")
    list_select_related = ("customer", "reservation")
    readonly_fields = ("created_at_display", "updated_at_display")

    @admin.display(ordering="status", description="Status")
    def status_badge(self, obj):
        color = {
            "paid": "green",
            "pending": "orange",
            "failed": "red",
            "card_saved": "blue",
        }.get(obj.status, "gray")
        return format_html(
            '<span style="color:{};font-weight:bold">{}</span>',
            color,
            obj.status.capitalize(),
        )

    @admin.display(description="Type")
    def payment_type(self, obj):
        return obj.get_payment_type_display()

    @admin.display(description="Reservation")
    def reservation_link(self, obj):
        if not obj.reservation_id:
            return "-"
        url = reverse(
            "admin:reservations_reservation_change", args=(obj.reservation_id,)
        )
        return format_html('<a href="{}">{}</a>', url, obj.reservation_id)

    @admin.display(ordering="amount", description="Amount")
    def amount_display(self, obj):
        return f"${obj.amount:.2f}"

    @admin.display(ordering="created_at", description="Created")
    def created_at_display(self, obj):
        return obj.created_at.strftime("%Y-%m-%d %H:%M")

    @admin.display(ordering="updated_at", description="Updated")
    def updated_at_display(self, obj):
        return obj.updated_at.strftime("%Y-%m-%d %H:%M")
