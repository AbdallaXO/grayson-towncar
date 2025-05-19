from django.contrib import admin
from django.utils.html import format_html
from django.urls import reverse
from django.db.models import Sum
from django.utils.safestring import mark_safe
from .models import Driver, DriverPayment, LegPayment
from reservations.models import Leg


@admin.register(Driver)
class DriverAdmin(admin.ModelAdmin):
    list_display = [
        "driver_name",
        "email",
        "vehicle",
        "unpaid_legs_count",
        "unpaid_amount",
        "total_paid",
        "total_legs",
    ]
    list_filter = ["profile__is_active"]
    search_fields = [
        "profile__username",
        "profile__first_name",
        "profile__last_name",
        "profile__email",
    ]

    fieldsets = (
        (
            "Driver Information",
            {"fields": ("profile", "vehicle", "schedule", "payment_method")},
        ),
        (
            "Payment Tracking",
            {
                "fields": (
                    "unpaid_legs_display",
                    "unpaid_amount_display",
                    "recent_leg_history",
                ),
                "description": "Payment status and leg history information.",
            },
        ),
    )
    readonly_fields = [
        "unpaid_legs_display",
        "unpaid_amount_display",
        "recent_leg_history",
    ]

    def driver_name(self, obj):
        return (
            f"{obj.profile.first_name} {obj.profile.last_name}"
            if obj.profile.first_name
            else obj.profile.username
        )

    driver_name.short_description = "Name"

    def email(self, obj):
        return obj.profile.email

    email.short_description = "Email"

    def unpaid_legs_count(self, obj):
        count = obj.get_unpaid_legs().count()
        if count > 0:
            url = (
                reverse("admin:reservations_leg_changelist")
                + f"?driver__id__exact={obj.id}&payment_status__exact=unpaid"
            )
            return format_html(
                '<a href="{}" style="color: red; font-weight: bold;">{}</a>', url, count
            )
        return format_html('<span style="color: green;">0</span>')

    unpaid_legs_count.short_description = "Unpaid Legs"

    def unpaid_amount(self, obj):
        amount = obj.get_total_unpaid_amount()
        if amount > 0:
            # Use string concatenation instead of format code
            return format_html(
                '<span style="color: red; font-weight: bold;">$'
                + str(amount)
                + "</span>"
            )
        return format_html('<span style="color: green;">$0.00</span>')

    unpaid_amount.short_description = "Unpaid Amount"

    def unpaid_legs_display(self, obj):
        legs = obj.get_unpaid_legs()
        if not legs:
            return "No unpaid legs"

        html = '<table style="width: 100%; border-collapse: collapse;">'
        html += '<tr style="background-color: #f0f0f0;"><th>Date</th><th>Route</th><th>Amount</th><th>Status</th></tr>'

        for leg in legs:
            html += "<tr>"
            html += f'<td><a href="{reverse("admin:reservations_leg_change", args=[leg.id])}">{leg.pickup_date}</a></td>'
            html += f"<td>{leg.pickup_location} to {leg.dropoff_location}</td>"
            # Format to avoid the decimal formatting issues
            amount = leg.driver_pay_amount or 0
            html += f"<td>${amount}</td>"
            html += f"<td>Unpaid</td>"
            html += "</tr>"

        html += "</table>"
        return mark_safe(html)

    unpaid_legs_display.short_description = "Unpaid Legs"

    def unpaid_amount_display(self, obj):
        amount = obj.get_total_unpaid_amount()
        return f"${amount}"

    unpaid_amount_display.short_description = "Unpaid Amount"

    def total_paid(self, obj):
        amount = obj.payments.aggregate(total=Sum("amount"))["total"] or 0
        # Use string concatenation instead of format code
        return format_html("$" + str(amount))

    total_paid.short_description = "Total Paid"

    def total_legs(self, obj):
        count = obj.legs.count()
        url = (
            reverse("admin:reservations_leg_changelist")
            + f"?driver__id__exact={obj.id}"
        )
        return format_html('<a href="{}">{}</a>', url, count)

    total_legs.short_description = "Total Legs"

    def recent_leg_history(self, obj):
        # Get the 10 most recent legs
        legs = obj.get_leg_history()[:10]
        if not legs:
            return "No legs found"

        html = '<table style="width: 100%; border-collapse: collapse;">'
        html += '<tr style="background-color: #f0f0f0;"><th>Date</th><th>Route</th><th>Amount</th><th>Status</th></tr>'

        for leg in legs:
            html += "<tr>"
            html += f'<td><a href="{reverse("admin:reservations_leg_change", args=[leg.id])}">{leg.pickup_date}</a></td>'
            html += f"<td>{leg.pickup_location} to {leg.dropoff_location}</td>"
            # Format to avoid the decimal formatting issues
            amount = leg.driver_pay_amount or 0
            html += f"<td>${amount}</td>"

            # Color-code the payment status
            if leg.payment_status == "paid":
                status_html = '<span style="color: green;">Paid</span>'
            elif leg.payment_status == "unpaid":
                status_html = '<span style="color: red;">Unpaid</span>'
            else:
                status_html = leg.payment_status.title()

            html += f"<td>{status_html}</td>"
            html += "</tr>"

        html += "</table>"
        html += f'<br><a href="{reverse("admin:reservations_leg_changelist")}?driver__id__exact={obj.id}">View all legs</a>'

        return mark_safe(html)

    recent_leg_history.short_description = "Recent Leg History"

    actions = ["preview_driver_payments", "process_driver_payments"]

    def preview_driver_payments(self, request, queryset):
        """Show a preview of driver payments without actually processing them."""
        from django.contrib import messages

        preview_data = []
        total_amount = 0

        for driver in queryset:
            # Get unpaid completed legs
            unpaid_legs = driver.get_unpaid_legs()

            if unpaid_legs:
                # Calculate total
                payment_total = sum(leg.driver_pay_amount or 0 for leg in unpaid_legs)

                # Count legs
                leg_count = unpaid_legs.count()

                # Get date range
                leg_dates = [leg.pickup_date for leg in unpaid_legs]
                if leg_dates:  # Check if there are actually dates to get min/max from
                    start_date = min(leg_dates)
                    end_date = max(leg_dates)

                    # Add to preview data
                    preview_data.append(
                        {
                            "driver": str(driver),
                            "amount": payment_total,
                            "count": leg_count,
                            "period": f"{start_date.strftime('%b %d, %Y')} to {end_date.strftime('%b %d, %Y')}",
                        }
                    )

                    total_amount += payment_total

        if preview_data:
            # Create a message with the preview data
            message = "Driver Payment Preview:<br><br>"
            message += "<table style='border-collapse: collapse; width: 100%;'>"
            message += "<tr style='background-color: #f2f2f2;'><th style='padding: 8px; text-align: left; border: 1px solid #ddd;'>Driver</th><th style='padding: 8px; text-align: left; border: 1px solid #ddd;'>Legs</th><th style='padding: 8px; text-align: left; border: 1px solid #ddd;'>Period</th><th style='padding: 8px; text-align: right; border: 1px solid #ddd;'>Amount</th></tr>"

            for item in preview_data:
                # Avoid the format code
                amt_str = str(item["amount"])
                message += f"<tr><td style='padding: 8px; border: 1px solid #ddd;'>{item['driver']}</td><td style='padding: 8px; border: 1px solid #ddd;'>{item['count']}</td><td style='padding: 8px; border: 1px solid #ddd;'>{item['period']}</td><td style='padding: 8px; text-align: right; border: 1px solid #ddd;'>${amt_str}</td></tr>"

            # Avoid the format code
            total_str = str(total_amount)
            message += f"<tr style='background-color: #f2f2f2;'><td colspan='3' style='padding: 8px; border: 1px solid #ddd; text-align: right;'><strong>Total:</strong></td><td style='padding: 8px; border: 1px solid #ddd; text-align: right;'><strong>${total_str}</strong></td></tr>"
            message += "</table><br>"
            message += "To process these payments, select the drivers again and use the 'Process driver payments' action."

            self.message_user(request, mark_safe(message))
        else:
            self.message_user(request, "No unpaid legs found for the selected drivers.")

    preview_driver_payments.short_description = "Preview driver payments"

    def process_driver_payments(self, request, queryset):
        """Process payments for all unpaid legs for selected drivers."""
        from django.contrib import messages
        from django.utils import timezone
        import logging

        logger = logging.getLogger(__name__)
        processed_count = 0
        total_amount = 0

        for driver in queryset:
            # Get unpaid completed legs
            unpaid_legs = driver.get_unpaid_legs()

            if unpaid_legs:
                # Calculate total
                payment_total = sum(leg.driver_pay_amount or 0 for leg in unpaid_legs)

                try:
                    # Create payment record
                    payment = DriverPayment.objects.create(
                        driver=driver,
                        amount=payment_total,
                        payment_method=driver.payment_method or "direct deposit",
                        reference_number=f"Auto-{timezone.now().strftime('%Y%m%d')}",
                        notes=f"Automatic payment for {unpaid_legs.count()} legs",
                        created_by=request.user,
                    )

                    # Create leg payment records and update legs
                    # Use direct updates to avoid signal recursion
                    for leg in unpaid_legs:
                        LegPayment.objects.create(
                            payment=payment, leg=leg, amount=leg.driver_pay_amount or 0
                        )

                        # Update leg status directly
                        Leg.objects.filter(id=leg.id).update(payment_status="paid")

                    processed_count += 1
                    total_amount += payment_total

                except Exception as e:
                    logger.error(f"Error processing payment for {driver}: {e}")
                    messages.error(request, f"Error processing {driver}: {e}")

        if processed_count:
            # Avoid the format code
            total_str = str(total_amount)
            messages.success(
                request,
                f"Processed payments for {processed_count} drivers. Total: ${total_str}",
            )
        else:
            messages.info(request, "No unpaid legs found for selected drivers.")

    process_driver_payments.short_description = "Process driver payments"


@admin.register(DriverPayment)
class DriverPaymentAdmin(admin.ModelAdmin):
    list_display = [
        "id",
        "driver_link",
        "amount_display",
        "payment_date",
        "payment_method",
        "leg_count",
    ]
    list_filter = ["payment_method", "payment_date"]
    search_fields = [
        "driver__profile__first_name",
        "driver__profile__last_name",
        "reference_number",
        "notes",
    ]
    date_hierarchy = "payment_date"

    def driver_link(self, obj):
        url = reverse("admin:drivers_driver_change", args=[obj.driver.id])
        return format_html('<a href="{}">{}</a>', url, obj.driver)

    driver_link.short_description = "Driver"

    def amount_display(self, obj):
        # Avoid format code
        return f"${obj.amount}"

    amount_display.short_description = "Amount"

    def leg_count(self, obj):
        count = obj.leg_payments.count()
        return count

    leg_count.short_description = "Legs Paid"

    readonly_fields = ["payment_date", "leg_details"]

    fieldsets = (
        (
            "Payment Information",
            {
                "fields": (
                    "driver",
                    "amount",
                    "payment_date",
                    "payment_method",
                    "reference_number",
                )
            },
        ),
        ("Notes", {"fields": ("notes",)}),
        ("Legs Paid", {"fields": ("leg_details",)}),
    )

    def leg_details(self, obj):
        leg_payments = obj.leg_payments.all().select_related("leg")
        if not leg_payments:
            return "No legs in this payment"

        html = '<table style="width: 100%; border-collapse: collapse;">'
        html += '<tr style="background-color: #f0f0f0;"><th>Leg ID</th><th>Date</th><th>Route</th><th>Amount</th></tr>'

        for lp in leg_payments:
            leg = lp.leg
            html += f"<tr>"
            html += f'<td><a href="{reverse("admin:reservations_leg_change", args=[leg.id])}">{leg.id}</a></td>'
            html += f"<td>{leg.pickup_date}</td>"
            html += f"<td>{leg.pickup_location} to {leg.dropoff_location}</td>"
            # Avoid format code
            html += f"<td>${lp.amount}</td>"
            html += f"</tr>"

        html += "</table>"
        return mark_safe(html)

    leg_details.short_description = "Leg Details"


# Use simple registration for LegPayment
@admin.register(LegPayment)
class LegPaymentAdmin(admin.ModelAdmin):
    list_display = ["id", "payment", "leg_display", "amount_display"]
    search_fields = [
        "payment__driver__profile__first_name",
        "payment__driver__profile__last_name",
    ]

    def leg_display(self, obj):
        return f"Leg #{obj.leg.id} - {obj.leg.pickup_date}"

    leg_display.short_description = "Leg"

    def amount_display(self, obj):
        # Avoid format code
        return f"${obj.amount}"

    amount_display.short_description = "Amount"
